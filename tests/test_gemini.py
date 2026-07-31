import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.config import ProviderConfig
from app.providers.gemini_provider import GeminiProvider
from app.providers.base import ProviderError
from app.schemas import ChatCompletionRequest, ChatMessage

@pytest.mark.asyncio
async def test_gemini_missing_api_key():
    config = ProviderConfig(base_url="https://test", api_key_env="MISSING_KEY")
    with patch.dict("os.environ", {}, clear=True):
        provider = GeminiProvider(config)
        request = ChatCompletionRequest(
            model="gemini-3.5-flash",
            messages=[ChatMessage(role="user", content="hello")]
        )
        with pytest.raises(ProviderError) as exc:
            await provider.chat_completion(request, "gemini-3.5-flash")
        assert "API key not configured" in str(exc.value)

@pytest.mark.asyncio
async def test_gemini_chat_completion_success():
    config = ProviderConfig(base_url="https://test", api_key_env="GEMINI_API_KEY")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "Hello user"}],
                    "role": "model"
                },
                "finishReason": "STOP",
                "index": 0
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 5,
            "candidatesTokenCount": 10,
            "totalTokenCount": 15
        }
    }
    
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
        provider = GeminiProvider(config)
        request = ChatCompletionRequest(
            model="gemini-3.5-flash",
            messages=[
                ChatMessage(role="system", content="you are an assistant"),
                ChatMessage(role="user", content="hello")
            ]
        )
        
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_resp
            resp = await provider.chat_completion(request, "gemini-3.5-flash")
            
            assert resp.choices[0].message.content == "Hello user"
            assert resp.usage.prompt_tokens == 5
            assert resp.usage.completion_tokens == 10
            
            args, kwargs = mock_post.call_args
            payload = kwargs["json"]
            assert payload["systemInstruction"]["parts"][0]["text"] == "you are an assistant"
            assert payload["contents"][0]["role"] == "user"
            assert payload["contents"][0]["parts"][0]["text"] == "hello"

@pytest.mark.asyncio
async def test_gemini_chat_completion_stream_success():
    config = ProviderConfig(base_url="https://test", api_key_env="GEMINI_API_KEY")
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    
    lines = [
        b'data: {"candidates": [{"content": {"parts": [{"text": "Hello"}]}, "finishReason": null, "index": 0}]}',
        b'data: {"candidates": [{"content": {"parts": [{"text": " world"}]}, "finishReason": "STOP", "index": 0}]}',
        b'data: [DONE]'
    ]
    
    async def mock_aiter_lines():
        for line in lines:
            yield line.decode("utf-8")
            
    mock_response.aiter_lines = mock_aiter_lines
    
    class MockContextManager:
        async def __aenter__(self):
            return mock_response
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
            
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
        provider = GeminiProvider(config)
        request = ChatCompletionRequest(
            model="gemini-3.5-flash",
            messages=[ChatMessage(role="user", content="hello")],
            stream=True
        )
        
        with patch("httpx.AsyncClient.stream") as mock_stream:
            mock_stream.return_value = MockContextManager()
            
            chunks = []
            async for chunk in provider.chat_completion_stream(request, "gemini-3.5-flash"):
                chunks.append(chunk)
                
            assert len(chunks) == 2
            assert chunks[0].choices[0].delta.content == "Hello"
            assert chunks[1].choices[0].delta.content == " world"
            assert chunks[1].choices[0].finish_reason == "stop"

@pytest.mark.asyncio
async def test_gemini_health_check():
    config = ProviderConfig(base_url="https://test", api_key_env="GEMINI_API_KEY")
    
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    
    mock_resp_404 = MagicMock()
    mock_resp_404.status_code = 404
    
    with patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"}):
        provider = GeminiProvider(config)
        
        # Test success (200)
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp_200
            res = await provider.health_check("gemini-3.5-flash", timeout=5.0)
            assert res.success is True
            
        # Test not found (404)
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_resp_404
            res = await provider.health_check("invalid-model", timeout=5.0)
            assert res.success is False
            assert "not found" in res.error
