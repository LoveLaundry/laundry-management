from . import customers, items, transactions, expenses, employees, payments, reports, dashboard, import_export, salary, advances, holidays, extra_work, company_settings

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
    salary.router,
    advances.router,
    holidays.router,
    extra_work.router,
    company_settings.router,
]