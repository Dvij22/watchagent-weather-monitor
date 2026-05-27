"""Services package."""

from app.services.event_detector import EventDetector
from app.services.weather_client import CITIES, WeatherClient

__all__ = ["WeatherClient", "CITIES", "EventDetector"]
