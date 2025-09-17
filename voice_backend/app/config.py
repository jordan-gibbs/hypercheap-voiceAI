from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8')

    # Fennec
    fennec_api_key: str
    fennec_sample_rate: int = 16000
    fennec_channels: int = 1

    # Baseten
    baseten_api_key: str
    baseten_base_url: str = "https://inference.baseten.co/v1"
    baseten_model: str = "meta-llama/Llama-4-Scout-17B-16E-Instruct"

    # Inworld TTS
    inworld_api_key: str
    inworld_model_id: str = "inworld-tts-1"
    inworld_voice_id: str = "Olivia"
    inworld_sample_rate: int = 48000

settings = Settings()
