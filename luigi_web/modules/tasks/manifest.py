from ...core.module_registry import Module, NavigationItem

module = Module(
    id="tasks", label="Tasks", description="Tasks, recurring work, reminders, and follow-ups.",
    router="luigi_web.modules.tasks.routes:router",
    startup="luigi_web.modules.tasks.routes:startup",
    package="luigi_web.modules.tasks",
    navigation=(NavigationItem("tasks", "Tasks", "/tasks", "list-checks", "Focus"),),
)