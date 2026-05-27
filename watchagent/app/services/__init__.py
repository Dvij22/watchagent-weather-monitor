"""Services package."""

from app.services.weather_client import CITIES, WeatherClient

__all__ = ["WeatherClient", "CITIES"]
