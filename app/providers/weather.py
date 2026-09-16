from app.providers.base import Provider, ProviderError, http_get_json

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WMO_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Dense drizzle",
    56: "Freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Violent showers",
    85: "Light snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with heavy hail",
}


class WeatherProvider(Provider):
    slug = "weather"
    name = "Weather (Open-Meteo)"
    version = "1.0.0"
    category = "weather"
    description = (
        "Live current-weather lookups via Open-Meteo: temperature, feels-like, "
        "humidity, wind, and conditions for any city. Keyless and read-only."
    )
    scopes = {"public:read"}

    def validate(self, args: dict) -> dict:
        location = str(args.get("location") or "").strip()
        if not location:
            raise ProviderError("location is required")
        if len(location) > 120:
            raise ProviderError("location is too long")
        units = str(args.get("units") or "celsius").strip().lower()
        if units not in ("celsius", "fahrenheit"):
            raise ProviderError("units must be 'celsius' or 'fahrenheit'")
        return {"location": location, "units": units}

    def execute(self, args: dict) -> dict:
        geo = http_get_json(GEOCODE_URL, {
            "name": args["location"], "count": 1,
            "language": "en", "format": "json",
        })
        results = geo.get("results") or []
        if not results:
            raise ProviderError(f"could not geocode location '{args['location']}'")
        place = results[0]
        params: dict = {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": (
                "temperature_2m,weather_code,relative_humidity_2m,"
                "apparent_temperature,wind_speed_10m,is_day"
            ),
            "temperature_unit": "fahrenheit" if args["units"] == "fahrenheit" else "celsius",
            "wind_speed_unit": "mph" if args["units"] == "fahrenheit" else "kmh",
        }
        timezone = place.get("timezone")
        if timezone:
            params["timezone"] = timezone
        forecast = http_get_json(FORECAST_URL, params)

        current = forecast.get("current") or {}
        units = forecast.get("current_units") or {}
        code = current.get("weather_code")
        display_name = place.get("name") or args["location"]
        region = place.get("country") or ""
        return {
            "location": f"{display_name}{', ' + region if region else ''}",
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "temperature": current.get("temperature_2m"),
            "temperature_unit": units.get("temperature_2m"),
            "feels_like": current.get("apparent_temperature"),
            "humidity_pct": current.get("relative_humidity_2m"),
            "wind_speed": current.get("wind_speed_10m"),
            "wind_speed_unit": units.get("wind_speed_10m"),
            "condition": WMO_CODES.get(code, "Unknown"),
            "weather_code": code,
            "is_day": bool(current.get("is_day", 1)),
            "observed_at": current.get("time"),
            "source": "Open-Meteo",
        }


provider = WeatherProvider()