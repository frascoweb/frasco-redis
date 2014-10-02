from frasco import Feature, action, current_app, current_context, g, hook, request, signal
from frasco.templating import jinja_fragment_extension
from redis import StrictRedis
import time
import inspect


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
            object.__setattr__(self, "_obj",self.loader())
        return self._obj

    def __getattr__(self, name):
        if name in self._cached_attrs:
            return self._cached_attrs[name]
        return getattr(self._load(), name)

    def __setattr__(self, name, value):
        if name in self._cached_attrs:
            del self._cached_attrs[name]
        setattr(self._load(), name, value)


class RedisFeature(Feature):
    name = "redis"
    ignore_attributes = ("connection",)
    defaults = {"url": "redis://localhost:6379/0",
                "view_cache_key_tpl": "{prefix}:{endpoint}",
                "view_cache_key_prefix": "views",
                "view_cache_timeout": 3600,
                "fragment_cache_timeout": None, # same as view cache
                "auto_model_cache_key": True,
                "cache_model_attrs": {}}

    def __init__(self, options=None):
        super(RedisFeature, self).__init__(options)

    def init_app(self, app):
        self.connection = StrictRedis.from_url(self.options["url"])
        app.jinja_env.add_extension(CacheFragmentExtension)
        self.register_redis_actions(app)

        if self.options["auto_model_cache_key"]:
            signal("model_inserted").connect(lambda _, obj: self.update_model_cache_key(obj), weak=False)
            signal("before_model_updated").connect(lambda _, obj: self.update_model_cache_key(obj, False), weak=False)

    def register_redis_actions(self, app):
        redis_ops = ("append", "bitop", "decr", "delete", "dump", "exists", "expire", "expireat", "get",
                     "getbit", "getrange", "getset", "incr", "incrby", "incrbyfloat", "keys", "mget",
                     "mset", "msetnx", "move", "persist", "pexpire", "pexpireat", "psetex", "pttl",
                     "randomkey", "rename", "renamenx", "restore", "set", "setbit", "setex", "setnx",
                     "setrange", "strlen", "substr", "ttl", "type", "blpop", "brpop", "brpoplpush",
                     "lindex", "linsert", "llen", "lpop", "lpush", "lpushx", "lrange", "lrem", "lset",
                     "ltrim", "rpop", "rpoplpush", "rpush", "rpushx", "sort", "scan", "sscan", "hscan",
                     "zscan", "sadd", "scard", "sdiff", "sdiffstore", "sinter", "sinterstore", "sismember",
                     "smembers", "smove", "spop", "srandmember", "srem", "sunion", "sunionstore", "zadd",
                     "zcard", "zcount", "zincrby", "zinterstore", "zlexcount", "zrange", "zrangebylex",
                     "zrangebyscore", "zrank", "zrem", "zremrangebylex", "zremrangebyrank", "zremrangebyscore",
                     "zrevrange", "zrevrangebyscore", "zrevrank", "zscore", "zunionstore", "pfadd", "pfcount",
                     "pfmerge", "hdel", "hexists", "hget", "hgetall", "hincrby", "hincrbyfloat", "hkeys",
                     "hlen", "hset", "hsetnx", "hmset", "hmget", "hvals", "publish", "eval", "evalsha",
                     "script_exists", "script_flush", "script_kill", "script_load")
        for op in redis_ops:
            app.actions.register(action("redis_" + op)(getattr(self.connection, op)))

    def update_model_cache_key(self, obj, save=True):
        obj.cache_key = "%s:%s" % (obj.id, time.time())
        self.cache_model_attributes(obj)
        if save:
            obj.save()

    def cache_model_attributes(self, obj):
        key = "models_attrs:%s:%s" % (obj.__class__.__name__, obj.id)
        attrs = dict((k, getattr(obj, k)) for k in self.options["cache_model_attrs"].get(obj.__class__.__name__, []))
        if attrs:
            self.connection.hmset(key, attrs)

    def get_cached_model_attributes(self, model, id):
        if inspect.isclass(model):
            model = model.__name__
        key = "models_attrs:%s:%s" % (model, id)
        return self.connection.hgetall(key) or {}

    def get_partial_model_from_cache(self, model, id):
        cached_attrs = self.get_cached_model_attributes(model, id)
        def loader():
            return current_app.features.models.query(model).get(id)
        return PartialObject(loader, dict(cached_attrs, id=id))

    def make_cache_key(self, key, ns=None, facets=None):
        if isinstance(key, (list, tuple)):
            key = ":".join(map(str, key))
        if isinstance(ns, (list, tuple)):
            ns = ":".join(map(str, ns))
        if ns:
            key = "%s:%s" % (ns, key)
        if facets:
            if isinstance(facets, dict):
                facets = ["%s=%s" % (k, v) for k, v in facets.iteritems()]
            key += ":%s" % ":".join(map(str, facets))
        return key

    def make_request_cache_key(self, key=None, ns=None, facets=None):
        key = key or self.options["view_cache_key_tpl"]
        key = key.format(
            prefix=self.options["view_cache_key_prefix"],
            endpoint=request.endpoint,
            path=request.path,
            method=request.method)
        return self.make_cache_key(key, facets, ns)

    @action("cached", default_option="timeout")
    def cache_view(self, timout=None, key=None, ns=None, facets=None):
        key = self.make_request_cache_key(key, facets, ns)
        data = self.connection.get(key)
        if data is not None:
            current_context.exit(data)
        g.redis_cache_view = (key, timout or self.options["view_cache_timeout"])
        return key

    @hook()
    def after_request(self, response):
        if "redis_cache_view" in g:
            response.freeze()
            self.connection.setex(g.redis_cache_view[0], g.redis_cache_view[1], "\n".join(response.response))
        return response

    @action(default_option="key")
    def clear_request_cache(self, key=None, ns=None, facets=None):
        self.connection.delete(self.make_request_cache_key(key, facets, ns))