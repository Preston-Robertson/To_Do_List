from ...core.module_registry import Module, NavigationItem

module = Module(
    id="preview", label="Preview", description="Separately unlocked deployment preview controls.",
    router="luigi_web.modules.preview.routes:router", package="luigi_web.modules.preview",
    navigation=(NavigationItem("preview", "Preview", "/admin/preview", "square-terminal", "System"),),
)