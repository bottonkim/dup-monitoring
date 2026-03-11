"""재시도 데코레이터"""
import time
import logging
import functools

logger = logging.getLogger(__name__)


def with_retry(max_attempts: int = 3, backoff_seconds: int = 5, exceptions=(Exception,)):
    """지수 백오프 재시도 데코레이터"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt < max_attempts:
                        wait = backoff_seconds * (2 ** (attempt - 1))
                        logger.warning(
                            f"{func.__name__} 실패 (시도 {attempt}/{max_attempts}), "
                            f"{wait}초 후 재시도: {e}"
                        )
                        time.sleep(wait)
                    else:
                        logger.error(f"{func.__name__} 최종 실패: {e}")
            raise last_exc
        return wrapper
    return decorator
