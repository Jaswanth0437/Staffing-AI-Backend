"""
FALLBACK employee seed — used only when the real Salesforce sync
(backend/salesforce_service.py) is unavailable (missing/invalid SF_* credentials
in .env). Same schema shape as a real synced employee row, so matching code
never needs to know which source populated the table.

Retry the real Salesforce integration once SF_USERNAME / SF_PASSWORD /
SF_SECURITY_TOKEN / SF_CONSUMER_KEY / SF_CONSUMER_SECRET are filled in, via
POST /admin/sync-employees.
"""

from sqlmodel import Session, select

from backend.models import Employee

MOCK_EMPLOYEES = [
    {"name": "Ava Chen", "role": "Backend Engineer", "skills": ["Python", "FastAPI", "PostgreSQL", "Docker"], "experience_summary": "6 years building REST APIs and data pipelines for SaaS platforms.", "seniority": "Senior"},
    {"name": "Liam Patel", "role": "Frontend Engineer", "skills": ["React", "TypeScript", "Next.js", "Tailwind CSS"], "experience_summary": "5 years shipping production web apps with a focus on performance.", "seniority": "Mid"},
    {"name": "Sofia Ramirez", "role": "Full Stack Engineer", "skills": ["Node.js", "React", "AWS", "GraphQL"], "experience_summary": "7 years across startups, comfortable owning features end-to-end.", "seniority": "Senior"},
    {"name": "Noah Kim", "role": "DevOps Engineer", "skills": ["Kubernetes", "Terraform", "AWS", "CI/CD"], "experience_summary": "8 years building and hardening cloud infrastructure.", "seniority": "Senior"},
    {"name": "Emma Johnson", "role": "Data Engineer", "skills": ["Python", "Airflow", "Spark", "SQL"], "experience_summary": "4 years building ETL pipelines for analytics teams.", "seniority": "Mid"},
    {"name": "Oliver Smith", "role": "Machine Learning Engineer", "skills": ["Python", "PyTorch", "MLOps", "AWS SageMaker"], "experience_summary": "5 years deploying ML models to production.", "seniority": "Mid"},
    {"name": "Isabella Garcia", "role": "Backend Engineer", "skills": ["Java", "Spring Boot", "Kafka", "PostgreSQL"], "experience_summary": "9 years on high-throughput distributed systems.", "seniority": "Senior"},
    {"name": "Ethan Brown", "role": "Mobile Engineer", "skills": ["Swift", "Kotlin", "React Native"], "experience_summary": "6 years shipping consumer mobile apps.", "seniority": "Senior"},
    {"name": "Mia Davis", "role": "QA Engineer", "skills": ["Selenium", "Cypress", "Python", "Test Automation"], "experience_summary": "5 years building automated test suites for web platforms.", "seniority": "Mid"},
    {"name": "Lucas Martinez", "role": "Backend Engineer", "skills": ["Go", "gRPC", "PostgreSQL", "Redis"], "experience_summary": "4 years building performance-critical microservices.", "seniority": "Mid"},
    {"name": "Charlotte Wilson", "role": "Frontend Engineer", "skills": ["Vue.js", "JavaScript", "CSS", "Figma"], "experience_summary": "3 years focused on design-system driven UI work.", "seniority": "Junior"},
    {"name": "James Anderson", "role": "Solutions Architect", "skills": ["AWS", "System Design", "Python", "Java"], "experience_summary": "12 years designing scalable enterprise architectures.", "seniority": "Staff"},
    {"name": "Amelia Thomas", "role": "Data Scientist", "skills": ["Python", "Pandas", "scikit-learn", "SQL"], "experience_summary": "4 years turning business questions into predictive models.", "seniority": "Mid"},
    {"name": "Benjamin Taylor", "role": "Security Engineer", "skills": ["Penetration Testing", "AWS", "Python", "OWASP"], "experience_summary": "7 years in application and cloud security.", "seniority": "Senior"},
    {"name": "Harper Moore", "role": "Full Stack Engineer", "skills": ["Python", "Django", "React", "PostgreSQL"], "experience_summary": "3 years building internal tools and customer-facing dashboards.", "seniority": "Junior"},
    {"name": "Henry Jackson", "role": "Backend Engineer", "skills": ["C#", ".NET", "Azure", "SQL Server"], "experience_summary": "8 years on enterprise line-of-business applications.", "seniority": "Senior"},
    {"name": "Evelyn White", "role": "Engineering Manager", "skills": ["Leadership", "Python", "System Design", "Agile"], "experience_summary": "10 years leading backend teams, still hands-on in code review.", "seniority": "Staff"},
    {"name": "Alexander Harris", "role": "Cloud Engineer", "skills": ["AWS", "GCP", "Terraform", "Kubernetes"], "experience_summary": "6 years migrating and running multi-cloud workloads.", "seniority": "Senior"},
]


def seed_mock_employees(session: Session) -> dict:
    existing_by_name = {e.name: e for e in session.exec(select(Employee)).all()}

    synced = 0
    for data in MOCK_EMPLOYEES:
        employee = existing_by_name.get(data["name"], Employee(**data))
        employee.role = data["role"]
        employee.skills = data["skills"]
        employee.experience_summary = data["experience_summary"]
        employee.seniority = data["seniority"]
        session.add(employee)
        synced += 1

    session.commit()
    return {"source": "mock_fallback", "synced": synced}
