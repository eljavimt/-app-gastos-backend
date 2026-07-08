from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import settings

# Crear motor de base de datos
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,  # Verifica la conexión antes de realizar operaciones
    pool_size=10,        # Número de conexiones permanentes en el pool
    max_overflow=20      # Conexiones adicionales permitidas en picos de carga
)

# Sesión local de base de datos
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Clase base declarativa para modelos
Base = declarative_base()

# Dependencia para inyectar la sesión de la base de datos en las rutas de FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
