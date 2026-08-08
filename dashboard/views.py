from django.http import JsonResponse


def health(request):
    return JsonResponse({
        "status": "ok",
        "service": "football-value-engine",
        "version": "1.0.0",
    })
