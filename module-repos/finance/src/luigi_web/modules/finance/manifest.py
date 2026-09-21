from ...core.module_registry import Module, NavigationItem

module = Module(
    id="finance", label="Finance", description="Separately unlocked accounts, budgets, and reports.",
    router="luigi_web.modules.finance.composition:router", package="luigi_web.modules.finance",
    startup="luigi_web.modules.finance.lifecycle:start",
    navigation=(NavigationItem("finance", "Finance", "/finance", "wallet", "Private", searchable=False),),
)