from django.http import JsonResponse
from django.shortcuts import render

from .services import DashboardService


def health(request):
    return JsonResponse({
        "status": "ok",
        "service": "football-value-engine",
        "version": "1.0.0",
    })


def dashboard_home(request):
    context = DashboardService().build()
    return render(request, "dashboard/index.html", context)
