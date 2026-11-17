from backend.core.ratelimit import TokenBuckets


def test_burst_then_rate():
    b = TokenBuckets(rate=2, burst=3)
    assert [b.take("a", now=0.0) for _ in range(3)] == [0.0, 0.0, 0.0]
    wait = b.take("a", now=0.0)
    assert 0 < wait <= 0.5                                      # empty: next token in 1/rate seconds
    assert b.take("a", now=0.5) == 0.0                          # refilled one token
    assert b.take("b", now=0.0) == 0.0                          # keys are independent


def test_memory_is_bounded():
    b = TokenBuckets(rate=1, burst=1, max_keys=100)
    for i in range(1000):
        b.take(str(i), now=float(i))
    assert len(b._b) <= 100
