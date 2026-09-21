from ...core.module_registry import Module, NavigationItem

module = Module(
    id="characters", label="Characters", description="Character sheets, level states, and rules references.",
    router="luigi_web.modules.characters.routes:router", package="luigi_web.modules.characters",
    startup="luigi_web.modules.characters.lifecycle:start",
    navigation=(NavigationItem("characters", "Characters", "/characters", "shield", "Library"),),
)