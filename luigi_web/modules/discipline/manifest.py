from ...core.module_registry import Module, NavigationItem

module = Module(
    id="discipline", label="Discipline", description="Habits, streaks, and completion history.",
    router="luigi_web.modules.discipline.routes:router", package="luigi_web.modules.discipline",
    navigation=(NavigationItem("discipline", "Discipline", "/discipline", "chart-no-axes-combined", "Planning"),),
)