if __package__:
    from .gpt_image_bridge.routes import register_routes
    from .nodes import (
        GPTImageBridgeEdit,
        GPTImageBridgeGenerate,
        GPTImageBridgeAPIProvider,
        GPTImageBridgeOAuthProvider,
        GPTImageBridgeReferenceList,
    )
else:
    from gpt_image_bridge.routes import register_routes
    from nodes import (
        GPTImageBridgeEdit,
        GPTImageBridgeGenerate,
        GPTImageBridgeAPIProvider,
        GPTImageBridgeOAuthProvider,
        GPTImageBridgeReferenceList,
    )

NODE_CLASS_MAPPINGS = {
    "GPTImageBridgeAPIProvider": GPTImageBridgeAPIProvider,
    "GPTImageBridgeOAuthProvider": GPTImageBridgeOAuthProvider,
    "GPTImageBridgeReferenceList": GPTImageBridgeReferenceList,
    "GPTImageBridgeGenerate": GPTImageBridgeGenerate,
    "GPTImageBridgeEdit": GPTImageBridgeEdit,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GPTImageBridgeAPIProvider": "GPT Image Bridge · API Provider",
    "GPTImageBridgeOAuthProvider": "GPT Image Bridge · Codex OAuth Provider",
    "GPTImageBridgeReferenceList": (
        "GPT Image Bridge · Edit Reference List (Image 2–9)"
    ),
    "GPTImageBridgeGenerate": "GPT Image Bridge · Generate",
    "GPTImageBridgeEdit": "GPT Image Bridge · Edit",
}

WEB_DIRECTORY = "./gpt_image_bridge/web"
register_routes()

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
