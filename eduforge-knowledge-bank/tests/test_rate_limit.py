from app.api.deps import get_rate_limiter
from app.core.rate_limit import MemoryRateLimiter, RedisRateLimiter


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_memory_limiter_blocks_then_resets_next_window():
    clock = Clock()
    rl = MemoryRateLimiter(2, 60, clock=clock)
    assert rl.hit("k").allowed and rl.hit("k").allowed
    third = rl.hit("k")
    assert not third.allowed and third.retry_after > 0
    assert rl.hit("other").allowed  # keys are independent
    clock.t += 61
    assert rl.hit("k").allowed


class FakePipe:
    def __init__(self, store, fail):
        self.store, self.fail, self.ops = store, fail, []

    def incr(self, k):
        self.ops.append(("incr", k))

    def expire(self, k, s):
        self.ops.append(("expire", k))

    def execute(self):
        if self.fail:
            raise ConnectionError("down")
        k = self.ops[0][1]
        self.store[k] = self.store.get(k, 0) + 1
        return [self.store[k], True]


class FakeRedisClient:
    def __init__(self, fail=False):
        self.store, self.fail = {}, fail

    def pipeline(self):
        return FakePipe(self.store, self.fail)


def test_redis_limiter_counts_and_blocks():
    rl = RedisRateLimiter(FakeRedisClient(), 2, 60, clock=Clock())
    assert [rl.hit("k").allowed for _ in range(3)] == [True, True, False]


def test_redis_limiter_fails_open_when_redis_down():
    assert RedisRateLimiter(FakeRedisClient(fail=True), 1, 60).hit("k").allowed


def test_api_returns_429_with_retry_after(fastapi_app, client, reader):
    fastapi_app.dependency_overrides[get_rate_limiter] = lambda: _LIMITER
    _LIMITER.reset()
    codes = [client.get("/api/v1/domains", headers=reader).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = client.get("/api/v1/domains", headers=reader)
    assert r.json()["error"]["code"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1


class _Limiter(MemoryRateLimiter):
    def reset(self):
        self._buckets.clear()


_LIMITER = _Limiter(2, 3600)
