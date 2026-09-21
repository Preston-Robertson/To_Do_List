from ...core.module_registry import Module, NavigationItem

module = Module(
    id="planning", label="Planning", description="Home, projects, calendar, and daily or weekly reviews.",
    router="luigi_web.modules.planning.routes:router", package="luigi_web.modules.planning",
    startup="luigi_web.modules.planning.routes:startup", requires=("tasks", "discipline"),
    navigation=(
        NavigationItem("home", "Home", "/home", "house", "Focus"),
        NavigationItem("projects", "Projects", "/projects", "chart-gantt", "Planning"),
        NavigationItem("calendar", "Calendar", "/calendar", "calendar-days", "Planning"),
        NavigationItem("review", "Review", "/review", "notebook-pen", "Planning"),
    ),
)