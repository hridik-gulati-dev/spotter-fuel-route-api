from django.urls import path

from router import views

urlpatterns = [
    path("api/route-fuel/", views.RouteFuelView.as_view(), name="route-fuel"),
    path("map/", views.map_page, name="map-page"),
    path("", views.map_page, name="home"),
]
