from slowapi import Limiter

from voice_cowork_backend.network import get_client_ip

limiter = Limiter(key_func=get_client_ip)
