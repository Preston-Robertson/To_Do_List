from ...core.module_registry import Module, NavigationItem

module = Module(
    id="admin", label="Administration", description="Host settings, integration health, and maintenance.",
    router="luigi_web.modules.admin.routes:router", package="luigi_web.modules.admin",
    navigation=(NavigationItem("admin", "Admin", "/admin", "settings-2", "System"),),
)