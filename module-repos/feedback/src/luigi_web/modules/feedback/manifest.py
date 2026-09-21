from ...core.module_registry import Module, NavigationItem

module = Module(
    id="feedback", label="Feedback", description="Local feedback and explicitly approved maintenance requests.",
    router="luigi_web.modules.feedback.routes:router", package="luigi_web.modules.feedback",
    startup="luigi_web.modules.feedback.lifecycle:start",
    navigation=(NavigationItem("feedback", "Feedback", "/feedback", "message-square", "System"),),
)