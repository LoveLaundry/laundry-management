from . import customers, items, transactions, expenses, employees, payments, reports, dashboard, import_export

routers = [
    customers.router,
    items.router,
    transactions.router,
    expenses.router,
    employees.router,
    payments.router,
    reports.router,
    dashboard.router,
    import_export.router,
]