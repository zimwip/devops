# {{SERVICE_NAME}}

{{DESCRIPTION}}

Owner: **{{OWNER}}** — Created: {{DATE}}

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
# → http://localhost:8080
# → http://localhost:8080/docs   (Swagger UI)
# → http://localhost:8080/health
```
