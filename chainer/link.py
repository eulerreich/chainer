import copy as copy_module
import sys

import numpy
import six

from chainer import cuda


class Link(object):

    def __init__(self, name=''):
        self.params = {}
        self.states = {}
        self._name = name

    @property
    def name(self):
        return self._name or '/'

    @name.setter
    def name(self, name):
        self._name = name if name != '/' else ''

    @property
    def volatile(self):
        for _, param in self.visitparams():
            return param.volatile
        return False

    @volatile.setter
    def volatile(self, value):
        value = bool(value)
        for _, param in self.visitparams():
            param.volatile = value

    def copy(self, shared=True):
        ret = copy_module.copy(self)

        copy = copy_module.copy if shared else copy_module.deepcopy
        ret.params = {}
        for key, param in six.iteritems(self.params):
            ret.params[key] = copy(param)
        ret.states = copy(self.states)
        return ret

    def to_cpu(self):
        for link in self.visitlinks():
            for param in six.itervalues(link.params):
                param.data = cuda.to_cpu(param.data)
                param._grad = cuda.to_cpu(param._grad)

            states = link.states
            for key, value in six.iteritems(states):
                states[key] = cuda.to_cpu(value)

        return self

    def to_gpu(self, device=None):
        cupy = cuda.cupy
        with cuda.get_device(device):
            for link in self.visitlinks():
                for param in six.itervalues(link.params):
                    param.data = cupy.asarray(param.data)
                    if param._grad is not None:
                        param._grad = cupy.asarray(param._grad)

                states = link.states
                for key, value in six.iteritems(states):
                    states[key] = cupy.asarray(value)

        return self

    def visitparams(self):
        for link in self.visitlinks():
            prefix = link._name + '/_params/'
            for key, param in six.iteritems(link.params):
                yield prefix + key, param

    def visitlinks(self):
        yield self

    def copyparams(self, link):
        params = {}
        for path, param in link.visitparams():
            params[path] = param.data
        for path, param in self.visitparams():
            dst = param.data
            src = params[path]
            if isinstance(dst, numpy.ndarray):
                numpy.copyto(dst, cuda.to_cpu(src))
            elif isinstance(src, numpy.ndarray):
                dst.set(src)
            else:
                cuda.cupy.copyto(dst, src)

    def zerograds(self):
        for link in self.visitlinks():
            params = link.params
            for key, p in six.iteritems(params):
                arr = p._grad
                if arr is None:
                    data = p.data
                    xp = cuda.get_array_module(data)
                    p._grad = xp.zeros_like(data)
                else:
                    arr.fill(0)

    def addgrads(self, link):
        grads = {}
        for path, param in link.visitparams():
            grads[path] = param._grad
        for path, param in self.visitparams():
            dst = param._grad
            src = grads[path]
            if isinstance(dst, numpy.ndarray):
                dst += cuda.to_cpu(src)
            elif isinstance(src, numpy.ndarray):
                dst += cuda.to_gpu(src, device=dst)
            elif src.device == dst.device:
                dst += src
            else:
                dst += cuda.copy(src, out_device=dst)

    def serialize(self, serializer):
        p = serializer['_params']
        for key, param in six.iteritems(self.params):
            param.data = p(key, param.data)
            # grad is not serialized

        states = self.states
        s = serializer['_states']
        for key, state in six.iteritems(states):
            states[key] = s(key, state)


class DictLink(Link):

    def __init__(self, **kwds):
        Link.__init__(self)
        self.children = kwds

        prefix = self._name + '/'
        for key, link in six.iteritems(kwds):
            if not isinstance(link, Link):
                raise TypeError('Cannot set a non-link object to DictLink')
            if link._name:
                raise ValueError('Cannot set a link to multiple parents')
            link.name = prefix + key

    def __contains__(self, key):
        return key in self.children

    def __delitem__(self, key):
        self.children[key].name = ''
        del self.children[key]

    def __iter__(self):
        return self.children.__iter__()

    def __getitem__(self, key):
        return self.children[key]

    def __len__(self):
        return len(self.children)

    def __setitem__(self, key, value):
        if not isinstance(value, Link):
            raise TypeError('Cannot set a non-link object to DictLink')
        if value._name:
            raise ValueError('Cannot set a link to multiple parents')
        value.name = '%s/%s' % (self._name, key)

        old = self.get(key, None)
        if old is not None:
            old.name = ''
        self.children[key] = value

    def clear(self):
        for link in six.itervalues(self.children):
            link.name = ''
        self.children.clear()

    def get(self, key, *args):
        return self.children.get(key, *args)

    def items(self):
        return self.children.items()

    if sys.version_info.major < 3:
        def iteritems(self):
            return self.children.iteritems()

        def iterkeys(self):
            return self.children.iterkeys()

        def itervalues(self):
            return self.children.itervalues()

    def has_key(self, key):
        return key in self

    def keys(self):
        return self.children.keys()

    def pop(self, key, *args):
        ret = self.children.pop(key, *args)
        if args and ret is args[0]:
            return ret
        ret.name = ''
        return ret

    def popitem(self):
        key, link = self.children.popitem()
        link.name = ''
        return key, link

    def setdefault(self, key, default=None):
        ret = self.children.get(key, None)
        if ret is None:
            self[key] = default
            return default
        else:
            return ret

    def values(self):
        return self.children.values()

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, name):
        if name == '/':
            name = ''
        self._name = name
        prefix = self._name + '/'
        for key, link in six.iteritems(self):
            link.name = prefix + key

    def copy(self, shared=True):
        ret = Link.copy(self, shared)
        for key, link in six.iteritems(self):
            ret[key] = link.copy(shared)
        return ret

    def visitlinks(self):
        yield self
        for o1 in six.itervalues(self):
            for o2 in o1.visitlinks():
                yield o2

    def serialize(self, serializer):
        Link.serialize(self, serializer)
        for key, link in six.iteritems(self):
            link.serialize(serializer[key])


class ListLink(Link):

    def __init__(self, *args):
        Link.__init__(self)
        for link in args:
            if not isinstance(link, Link):
                raise TypeError('Cannot set a non-link object to ListLink')
            if link._name:
                raise ValueError('Cannot set a link to multiple parents')
        for i, link in enumerate(args):
            link.name = '%s/%d' % (self._name, i)
        self.children = list(args)

    def __getitem__(self, idx):
        return self.children[idx]

    def __iter__(self):
        return self.children.__iter__()

    def __len__(self):
        return len(self.children)

    def __setitem__(self, idx, value):
        if not isinstance(value, Link):
            raise TypeError('Cannot set a non-link object to ListLink')
        if value._name:
            raise ValueError('Cannot set a link to multiple parents')
        value.name = '%s/%d' % (self._name, idx)

        self.children[idx].name = ''
        self.children[idx] = value

    def append(self, link):
        if not isinstance(link, Link):
            raise TypeError('Cannot set a non-link object to ListLink')
        if link._name:
            raise ValueError('Cannot set a link to multiple parents')
        link.name = '%s/%d' % (self._name, len(self.children))
        self.children.append(link)

    def pop(self):
        link = self.children.pop()
        link.name = ''
        return link

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, name):
        self._name = name
        for i, link in enumerate(self):
            link.name = '%s/%d' % (name, i)

    def copy(self, shared=True):
        ret = Link.copy(self, shared)
        for i, link in enumerate(self):
            ret[i] = link.copy(shared)
        return ret

    def visitlinks(self):
        yield self
        for l1 in self:
            for l2 in l1.visitlinks():
                yield l2

    def serialize(self, serializer):
        Link.serialize(self, serializer)
        for idx, link in enumerate(self):
            link.serialize(serializer[str(idx)])


def _apply_on_variable(v, func):
    v.data = func(v.data)
    if v._grad is not None:
        v._grad = func(v._grad)