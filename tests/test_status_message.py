"""Tests for StatusMessage Slack live-update widget."""

import asyncio
import json
import time

import pytest
from unittest.mock import AsyncMock, MagicMock


class TestStatusMessage:
    def _make_status_message(self):
        from sysop.bot import StatusMessage
        return StatusMessage(channel="C123", thread_ts="T456")

    @pytest.mark.asyncio
    async def test_post_initial(self):
        status = self._make_status_message()
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "M789"}
        await status.post_initial(client)
        client.chat_postMessage.assert_called_once()
        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C123"
        assert call_kwargs["thread_ts"] == "T456"
        assert "Working" in call_kwargs["text"]
        assert status.message_ts == "M789"

    @pytest.mark.asyncio
    async def test_append_and_flush(self):
        status = self._make_status_message()
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "M789"}
        client.chat_update.return_value = {}
        await status.post_initial(client)
        await status.append("Running: `kubectl get pods`", force=True)
        client.chat_update.assert_called_once()
        call_kwargs = client.chat_update.call_args.kwargs
        assert "kubectl get pods" in call_kwargs["text"]
        assert call_kwargs["ts"] == "M789"

    @pytest.mark.asyncio
    async def test_debounce_skips_rapid_updates(self):
        status = self._make_status_message()
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "M789"}
        client.chat_update.return_value = {}
        await status.post_initial(client)
        await status.append("Line 1", force=True)
        count_after_first = client.chat_update.call_count
        await status.append("Line 2", force=False)
        assert client.chat_update.call_count == count_after_first

    @pytest.mark.asyncio
    async def test_force_bypasses_debounce(self):
        status = self._make_status_message()
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "M789"}
        client.chat_update.return_value = {}
        await status.post_initial(client)
        await status.append("Line 1", force=True)
        count_after_first = client.chat_update.call_count
        await status.append("Line 2", force=True)
        assert client.chat_update.call_count == count_after_first + 1

    @pytest.mark.asyncio
    async def test_finalize_success(self):
        status = self._make_status_message()
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "M789"}
        client.chat_update.return_value = {}
        await status.post_initial(client)
        await status.append("Step 1", force=True)
        await status.finalize(success=True)
        last_call = client.chat_update.call_args.kwargs
        assert "Done" in last_call["text"]

    @pytest.mark.asyncio
    async def test_finalize_failure(self):
        status = self._make_status_message()
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "M789"}
        client.chat_update.return_value = {}
        await status.post_initial(client)
        await status.finalize(success=False)
        last_call = client.chat_update.call_args.kwargs
        assert "Failed" in last_call["text"]

    @pytest.mark.asyncio
    async def test_line_cap_at_20(self):
        status = self._make_status_message()
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "M789"}
        client.chat_update.return_value = {}
        await status.post_initial(client)
        for i in range(25):
            await status.append(f"Step {i}", force=True)
        last_call = client.chat_update.call_args.kwargs
        text = last_call["text"]
        assert "5 earlier steps" in text
        assert "Step 24" in text
        assert "Step 23" in text
        assert "Step 0" not in text
        assert "Step 4" not in text

    @pytest.mark.asyncio
    async def test_flush_retries_on_rate_limit(self):
        from slack_sdk.errors import SlackApiError
        status = self._make_status_message()
        client = AsyncMock()
        client.chat_postMessage.return_value = {"ts": "M789"}
        error_response = MagicMock()
        error_response.status_code = 429
        error_response.headers = {"Retry-After": "0"}
        client.chat_update.side_effect = [
            SlackApiError("ratelimited", error_response),
            {},
        ]
        await status.post_initial(client)
        await status.append("Line 1", force=True)
        assert client.chat_update.call_count == 2

    @pytest.mark.asyncio
    async def test_finalize_without_post_initial_is_noop(self):
        status = self._make_status_message()
        await status.finalize(success=True)
        # No exception, no client calls — just a no-op
