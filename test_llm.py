import asyncio
import os
from dotenv import load_dotenv
import httpx

load_dotenv()


async def main() -> None:
    api_key = os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY", "")
    base_url = (os.getenv("BASE_URL") or os.getenv("OPENAI_BASE_URL", "https://opencode.ai/zen/go/v1")).rstrip("/")
    model = os.getenv("MODEL") or os.getenv("OPENAI_MODEL", "deepseek-v4-flash-free")

    if not api_key:
        print("ОШИБКА: API_KEY не задан в .env")
        return

    print(f"Модель: {model}")
    print(f"URL:    {base_url}/chat/completions")
    print()

    async with httpx.AsyncClient(timeout=30) as client:
        payload = {
            "model": model,
            "messages": [
                {"role": "user", "content": "Привет! Ответь одним словом."},
            ],
            "temperature": 0.5,
        }

        try:
            response = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            print("УСПЕХ! Ответ модели:")
            print(f"  {content}")
        except Exception as e:
            print(f"ОШИБКА: {e}")


if __name__ == "__main__":
    asyncio.run(main())
