from frasco import current_app, json
from frasco.utils import unknown_value
from frasco.templating import jinja_fragment_extension
import re
import inspect


__all__ = ('CacheFragmentExtension', 'PartialObject', 'redis_get_set', 'redis_get_set_as_json',
           'redis_cached_function', 'unknown_value', 'redis_cached_property', 'redis_cached_property_as_json',
           'redis_cached_method', 'redis_cached_method_as_json', 'RedisHash', 'JSONRedisHash',
           'RedisList', 'JSONRedisList', 'RedisSet', 'JSONRedisSet', 'build_object_key')


@jinja_fragment_extension("cache")
def CacheFragmentExtension(name=None, caller=None, timeout=None, key=None, model=None, facets=None, ns=None):
    conn = current_app.features.redis.connection
    if model:
        if not getattr(model, 'cache_key', None):
            current_app.features.redis.update_model_cache_key(model)
        key = model.cache_key
    if not facets:
        facets = []
    if name:
        facets.insert(0, name)
    key = current_app.features.redis.make_request_cache_key(key, ns, facets)
    rv = conn.get(key)
    if rv is None:
        timeout = timeout or current_app.features.redis.options["fragment_cache_timeout"]\
            or current_app.features.redis.options["view_cache_timeout"]
        rv = caller()
        conn.setex(key, timeout, rv)
    return rv


class PartialObject(object):
    def __init__(self, loader, cached_attrs=None):
        object.__setattr__(self, '_loader', loader)
        object.__setattr__(self, "_obj", None)
        object.__setattr__(self, "_cached_attrs", dict(cached_attrs or {}))

    def _load(self):
        if not self._obj:
            object.__setattr__(self, "_obj", self.loader())
        return self._obj

    def __getattr__(self, name):
        if name in self._cached_attrs:
            return self._cached_attrs[name]
        return getattr(self._load(), name)

    def __setattr__(self, name, value):
        if name in self._cached_attrs:
            del self._cached_attrs[name]
        setattr(self._load(), name, value)


def redis_get_set(key, callback, ttl=None, coerce=None, serializer=None, redis=None):
    if not redis:
        redis = current_app.features.redis.connection
    if redis.exists(key):
        value = redis.get(key)
        if coerce:
            return coerce(value)
        return value
    _value = value = callback()
    if serializer:
        _value = serializer(value)
    if ttl:
        redis.setex(key, ttl, _value)
    else:
        redis.set(key, _value)
    return value


def redis_get_set_as_json(key, callback, **kwargs):
    kwargs['serializer'] = json.dumps
    kwargs['coerce'] = json.loads
    return redis_get_set(key, callback, **kwargs)


def redis_cached_function(key, **opts):
    def decorator(func):
        def wrapper(*args, **kwargs):
            return redis_get_set(key, func, **opts)
        return wrapper
    return decorator


def build_object_key(obj, name=None, key=None, at_values=None):
    super_key = getattr(obj, '__redis_cache_key__', None)
    if key and '{__super__}' in key and super_key is not None:
        key = key.replace('{__super__}', super_key)
    elif not key and super_key:
        key = super_key
    elif not key:
        key = '%s:{__name__}' % obj.__class__.__name__
    attributes = {}
    for attr in re.findall(r'\{(@?[a-z0-9_]+)[^}]*\}', key, re.I):
        value = ''
        if attr == '__name__':
            value = name or obj.__class__.__name__
        elif attr.startswith('@') and at_values:
            value = at_values.get(attr[1:], '')
        else:
            value = getattr(obj, attr)
        if value:
            cache_id = getattr(value, '__redis_cache_id__', None)
            if cache_id:
                value = cache_id()
        attributes[attr] = value
    return key.format(**attributes)


class RedisCachedAttribute(object):
    def __init__(self, func, redis=None, key=None, ttl=None, coerce=None,\
                 serializer=None, name=None):
        self.func = func
        self.__name__ = func.__name__
        self.__doc__ = func.__doc__
        self._redis = redis
        self.key = key
        self.ttl = ttl
        self.coerce = coerce
        self.serializer = serializer
        self.name = name or self.__name__
        self.cached_property_name = self.__name__ + '_cached'
        self.cache_disabled = False
        self.cache_ignore_current = False
        self.cache_current_ttl = None

    @property
    def redis(self):
        return self._redis or current_app.features.redis.connection

    def _set_cached_value(self, key, value, default_ttl=None):
        if self.serializer:
            value = self.serializer.dumps(value)
        ttl = self.cache_current_ttl
        if ttl is None:
            ttl = default_ttl
        if ttl is not None:
            self.redis.setex(key, ttl, value)
        else:
            self.redis.set(key, value)

    def _get_cached_value(self, key):
        if not self.redis.exists(key):
            return unknown_value
        value = self.redis.get(key)
        if value is None:
            return None
        if self.serializer:
            value = self.serializer.loads(value)
        if self.coerce:
            value = self.coerce(value)
        return value

    def _call_func(self, obj, *args, **kwargs):
        self.cache_ignore_current = False
        self.cache_current_ttl = self.ttl
        return self.func(obj, *args, **kwargs)


class RedisCachedProperty(RedisCachedAttribute):
    def __init__(self, func, fset=None, fdel=None, **kwargs):
        super(RedisCachedProperty, self).__init__(func, **kwargs)
        self.fset = fset
        self.fdel = fdel

    def __get__(self, obj, cls):
        if obj is None:
            return self
        value = obj.__dict__.get(self.cached_property_name, unknown_value)
        if value is unknown_value:
            key = None
            if not self.cache_disabled:
                try:
                    key = self.build_key(obj)
                    value = self._get_cached_value(key)
                except Exception as e:
                    current_app.logger.error(e)
                    value = unknown_value
            if value is unknown_value:
                value = self.get_fresh(obj)
                if not self.cache_disabled and not self.cache_ignore_current and key:
                    self._set_cached_value(key, value,
                        getattr(obj, '__redis_cache_ttl__', None))
            obj.__dict__[self.cached_property_name] = value
        return value

    def __set__(self, obj, value):
        if self.fset is None:
            raise AttributeError("can't set attribute")
        self.fset(obj, value)
        self.invalidate(obj)

    def __delete__(self, obj):
        if self.fdel is None:
            raise AttributeError("can't delete attribute")
        self.fdel(obj)
        self.invalidate(obj)

    def build_key(self, obj):
        return build_object_key(obj, self.name, self.key)

    def get_cached(self, obj):
        try:
            key = self.build_key(obj)
        except Exception as e:
            current_app.logger.error(e)
            return unknown_value
        return self._get_cached_value(key)

    def get_fresh(self, obj):
        return self._call_func(obj)

    def require_fresh(self, obj):
        obj.__dict__.pop(self.cached_property_name, None)

    def invalidate(self, obj):
        try:
            key = self.build_key(obj)
        except Exception as e:
            current_app.logger.error(e)
            return
        self.redis.delete(key)

    def setter(self, fset):
        self.fset = fset
        return self

    def deleter(self, fdel):
        self.fdel = fdel
        return self


def redis_cached_property(fget=None, **kwargs):
    def wrapper(f):
        return RedisCachedProperty(f, **kwargs)
    if fget:
        return wrapper(fget)
    return wrapper


def redis_cached_property_as_json(fget=None, **kwargs):
    kwargs['serializer'] = json
    return redis_cached_property(fget, **kwargs)


class RedisCachedMethod(RedisCachedAttribute):
    def __get__(self, obj, cls=None):
        self.obj = obj
        return self

    def __call__(self, *args, **kwargs):
        obj = kwargs.pop('__obj__', self.obj)
        value = unknown_value
        if not self.cache_disabled:
            key = None
            try:
                key = self.build_key(args, kwargs, obj)
                value = self._get_cached_value(key)
            except Exception as e:
                current_app.logger.error(e)
                value = unknown_value
        if value is unknown_value:
            value = self._call_func(obj, *args, **kwargs)
            if not self.cache_disabled and not self.cache_ignore_current and key:
                self._set_cached_value(key, value,
                    getattr(self.obj, '__redis_cache_ttl__', None))
        return value

    def cached(self, *args, **kwargs):
        obj = kwargs.pop('__obj__', self.obj)
        try:
            key = self.build_key(args, kwargs, obj)
        except Exception as e:
            current_app.logger.error(e)
            return unknown_value
        return self._get_cached_value(key)

    def fresh(self, *args, **kwargs):
        obj = kwargs.pop('__obj__', self.obj)
        return self._call_func(obj, *args, **kwargs)

    def invalidate(self, *args, **kwargs):
        obj = kwargs.pop('__obj__', self.obj)
        try:
            key = self.build_key(args, kwargs, obj)
        except Exception as e:
            current_app.logger.error(e)
            return
        self.redis.delete(key)

    def build_key(self, args=None, kwargs=None, obj=None):
        if not obj:
            obj = self.obj
        if not args:
            args = []
        if not kwargs:
            kwargs = {}
        at_values = inspect.getcallargs(self.func, obj, *args, **kwargs)
        return build_object_key(obj, self.name, self.key, at_values)


def redis_cached_method(func=None, **kwargs):
    def wrapper(f):
        return RedisCachedMethod(f, **kwargs)
    if func:
        return wrapper(func)
    return wrapper


def redis_cached_method_as_json(func=None, **kwargs):
    kwargs['serializer'] = json
    return redis_cached_method(func, **kwargs)


class RedisObject(object):
    def __init__(self, key, serializer=None, coerce=None, redis=None):
        self.key = key
        self.serializer = serializer
        self.coerce = coerce
        self.redis = redis or current_app.features.redis.connection

    def _to_redis(self, value):
        if self.serializer:
            return self.serializer.dumps(value)
        return value

    def _from_redis(self, value):
        if self.serializer:
            value = self.serializer.loads(value)
        if self.coerce:
            value = self.coerce(value)
        return value

    def clear(self):
        self.redis.delete(self.key)

    def expire(self, ttl):
        self.redis.expire(self.key, ttl)


class RedisHash(RedisObject):
    def __setitem__(self, key, value):
        self.redis.hset(self.key, key, self._to_redis(value))

    def __getitem__(self, key):
        return self._from_redis(self.redis.hget(self.key, key))

    def __delitem__(self, key):
        return self.redis.hdel(self.key, key)

    def __contains__(self, key):
        return key in self.keys()

    def keys(self):
        return self.redis.hkeys(self.key)

    def items(self):
        return {k: self._from_redis(v) for k, v in self.redis.hgetall(self.key).iteritems()}

    def values(self):
        return self.items().values()

    def update(self, dct):
        pipe = self.redis.pipeline()
        for k, v in dct.iteritems():
            pipe.hset(self.key, k, self._to_redis(v))
        pipe.execute()


class JSONRedisHash(RedisHash):
    def __init__(self, key, **kwargs):
        kwargs['serializer'] = json
        super(JSONRedisHash, self).__init__(key, **kwargs)


class RedisList(RedisObject):
    def __setitem__(self, index, value):
        self.redis.lset(self.key, index, self._to_redis(value))

    def __getitem__(self, index):
        if isinstance(index, slice):
            if slice.step is not None:
                return [self[i] for i in xrange(*index.indices(len(self)))]
            return [self._from_redis(v) for v in \
                self.redis.lrange(self.key, slice.start or 0, slice.stop or -1)]
        elif isinstance(index, int):
            return self._from_redis(self.redis.lindex(self.key, index))
        else:
            raise TypeError("Invalid argument type.")

    def __len__(self):
        return self.redis.llen(self.key)

    def __iter__(self):
        for value in self.redis.lrange(self.key, 0, -1):
            yield self._from_redis(value)

    def __contains__(self, value):
        return value in list(self)

    def append(self, value):
        self.redis.rpush(self.key, self._to_redis(value))

    def extend(self, lst):
        pipe = self.redis.pipeline()
        for v in lst:
            pipe.rpush(self.key, self._to_redis(v))
        pipe.execute()

    def remove(self, value):
        self.redis.lrem(self.key, self._to_redis(value))


class JSONRedisList(RedisList):
    def __init__(self, key, **kwargs):
        kwargs['serializer'] = json
        super(JSONRedisList, self).__init__(key, **kwargs)


class RedisSet(RedisObject):
    def __iter__(self):
        for value in self.redis.smembers(self.key):
            yield self._from_redis(value)

    def __contains__(self, value):
        return self.redis.ismember(self.key, value)

    def add(self, value):
        self.redis.sadd(self.key, self._to_redis(value))

    def update(self, lst):
        pipe = self.redis.pipeline()
        for v in lst:
            pipe.sadd(self.key, self._to_redis(v))
        pipe.execute()

    def remove(self, value):
        self.redis.srem(self.key, self._to_redis(value))

    def pop(self):
        return self._from_redis(self.redis.spop(self.key))

    def move(self, destination, value):
        if isinstance(destination, RedisSet):
            destination = destination.key
        self.redis.smove(self.key, self.destination, self._to_redis(value))

    def diff(self, *other_keys):
        return self._cmp('sdiff', other_keys)

    def inter(self, *other_keys):
        return self._cmp('sinter', other_keys)

    def union(self, *other_keys):
        return self._cmp('union', other_keys)

    def _cmp(self, op, other_keys):
        keys = []
        for k in other_keys:
            if isinstance(k, RedisSet):
                keys.append(k.key)
            else:
                keys.append(k)
        for value in getattr(self.redis, op)(self.key, *keys):
            yield self._from_redis(value)


class JSONRedisSet(RedisSet):
    def __init__(self, key, **kwargs):
        kwargs['serializer'] = json
        super(JSONRedisSet, self).__init__(key, **kwargs)