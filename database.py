from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# URL de conexión a Neon PostgreSQL
URL_BASE_DATOS = "postgresql://neondb_owner:npg_RqOsroY5JuE4@ep-shiny-tree-ax2ijt0o-pooler.c-4.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"

# Conexión a PostgreSQL (Neon) con reconexión automática anti-desconexión
engine = create_engine(
    URL_BASE_DATOS,
    pool_pre_ping=True,  # Verifica si la conexión sigue viva antes de cada consulta
    pool_recycle=300,    # Renueva las conexiones cada 5 minutos
    pool_size=5,
    max_overflow=10
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()