import base64
import unittest
from unittest.mock import AsyncMock, patch
from ai_secretary import rpc


class RPCTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_schema_documents_guard_and_identity(self):
        result = await rpc.dispatch({"method": "tools/list"})
        schema = next(x["inputSchema"] for x in result["tools"] if x["name"] == "secretary_act")
        self.assertIn("expected", schema["required"])
        self.assertIn("operation_id", schema["required"])
        self.assertIn("click", schema["properties"]["action"]["enum"])
    async def test_observation_reaches_vision_as_image_not_base64_text(self):
        encoded = base64.b64encode(b"synthetic PNG fixture").decode()
        native = {"ok": True, "result": {"observation": {"token": "guard"}, "screenshot": {"format": "png", "base64": encoded}}}
        with patch.object(rpc, "handle", AsyncMock(return_value=native)):
            result = await rpc.dispatch({"method": "tools/call", "params": {"name": "secretary_observe", "arguments": {"pid": 1}}})
        images = [x for x in result["content"] if x["type"] == "image"]
        self.assertEqual(images, [{"type": "image", "mimeType": "image/png", "data": encoded}])
        self.assertNotIn(encoded, result["content"][0]["text"])
        self.assertIn("guard", result["content"][0]["text"])

if __name__ == "__main__": unittest.main()
