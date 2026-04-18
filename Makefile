.PHONY: run-api run-admin run-tests

run-api:
    cd api && uvicorn main:app --host 0.0.0.0 --port 8000 --reload

run-admin:
    cd admin_panel && python manage.py runserver 8001

run-tests:
    pytest tests/ -v