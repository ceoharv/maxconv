import os
from dataclasses import dataclass, field


@dataclass
class Config:
    bot_token: str = os.getenv(
        "BOT_TOKEN", "9058128007:AAEXz-22VaVejWO3XR1rlWgrOB4LOIgDgpk"
    )
    # Адрес авторизации MAX. Оставь пустым -- автопоиск найдёт сам
    # и сохранит в found_endpoint.json, потом подхватится отсюда.
    max_auth_url: str = os.getenv("MAX_AUTH_URL", "")

    android_user_agent: str = "MAX/24.10.1 (Android 14; ru)"
    android_client_id: str = "android"
    android_app_version: str = "24.10.1"

    allowed_users: list[int] = field(default_factory=list)


config = Config()