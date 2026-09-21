from ...core.module_registry import Module

module = Module(
    id="assistant", label="Assistant", description="Task and media chat with a bounded tool registry.",
    router="luigi_web.modules.assistant.routes:router", package="luigi_web.modules.assistant",
    requires=("tasks", "discipline", "media"),
)