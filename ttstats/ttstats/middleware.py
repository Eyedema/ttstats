from threading import local

_thread_locals = local()


def get_current_user():
    """
    Get the current user from thread-local storage.
    Used by custom managers to filter querysets automatically.
    """
    return getattr(_thread_locals, 'user', None)


class CurrentUserMiddleware:
    """
    Middleware that stores the current request user in thread-local storage.
    This enables automatic row-level security filtering in model managers.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Store current user in thread-local storage
        _thread_locals.user = getattr(request, 'user', None)
        
        try:
            response = self.get_response(request)
        finally:
            # Clean up thread-local storage after request
            if hasattr(_thread_locals, 'user'):
                del _thread_locals.user
        
        return response
