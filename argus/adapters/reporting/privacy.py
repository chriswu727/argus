from functools import wraps

from argus.security import redacted_copy


def safe_report(render):
    @wraps(render)
    def wrapped(result, *args, **kwargs):
        return render(redacted_copy(result), *args, **kwargs)

    return wrapped
