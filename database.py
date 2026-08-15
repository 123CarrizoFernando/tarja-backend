from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# ¡REEMPLAZÁ ESTO POR TU URL DE NEON.TECH!
# Debe empezar con postgresql://...
URL_BASE_DATOS = "postgresql://neondb_owner:npg_RqOsroY5JuE4@ep-shiny-tree-ax2ijt0o-pooler.c-4.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

# Conexión a PostgreSQL (Neon)
engine = create_engine(URL_BASE_DATOS)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()