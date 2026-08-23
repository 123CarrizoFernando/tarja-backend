from sqlalchemy import Column, Integer, String, ForeignKey, Date, Time, Boolean
from sqlalchemy.orm import relationship
from database import Base

class Sector(Base):
    __tablename__ = "sectores"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, unique=True, index=True)

    encargados = relationship("Encargado", back_populates="sector")
    empleados = relationship("Empleado", back_populates="sector")

class Encargado(Base):
    __tablename__ = "encargados"
    id = Column(Integer, primary_key=True, index=True)
    usuario = Column(String, unique=True, index=True)
    password_hash = Column(String)
    sector_id = Column(Integer, ForeignKey("sectores.id"), nullable=True) # Ahora puede ser nulo para el Admin
    rol = Column(String, default="encargado") # NUEVO: "admin" o "encargado"

    sector = relationship("Sector", back_populates="encargados")

class Empleado(Base):
    __tablename__ = "empleados"
    id = Column(Integer, primary_key=True, index=True)
    dni = Column(String, unique=True, index=True)
    nombre_completo = Column(String)
    legajo = Column(String, unique=True, index=True)
    sector_id = Column(Integer, ForeignKey("sectores.id"))

    sector = relationship("Sector", back_populates="empleados")
    asistencias = relationship("Asistencia", back_populates="empleado")
    activo = Column(Boolean, default=True) # NUEVO: Para dar de baja sin borrar

class Asistencia(Base):
    __tablename__ = "asistencias"

    id = Column(Integer, primary_key=True, index=True)
    empleado_id = Column(Integer, ForeignKey("empleados.id"))
    fecha = Column(Date)
    hora_llegada = Column(String, nullable=True)
    hora_salida = Column(String, nullable=True)
    estado = Column(String, default="Presente") # Ejemplo: "Presente", "Ausente", "Licencia"
    sincronizado = Column(Boolean, default=False)

    empleado = relationship("Empleado", back_populates="asistencias")