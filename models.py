from sqlalchemy import Column, Integer, String, ForeignKey, Date, Time, Boolean, Text
from sqlalchemy.orm import relationship
from database import Base

class Secretaria(Base):
    __tablename__ = "secretarias"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, unique=True, index=True)

class Funcion(Base):
    __tablename__ = "funciones"
    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, unique=True, index=True)

class Feriado(Base):
    __tablename__ = "feriados"
    id = Column(Integer, primary_key=True, index=True)
    fecha = Column(Date, unique=True, index=True)
    descripcion = Column(String)

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
    nombre_completo = Column(String, nullable=True)
    password_hash = Column(String)
    sector_id = Column(Integer, ForeignKey("sectores.id"), nullable=True)
    rol = Column(String, default="encargado")
    activo = Column(Boolean, default=True)

    sector = relationship("Sector", back_populates="encargados")

class Empleado(Base):
    __tablename__ = "empleados"
    id = Column(Integer, primary_key=True, index=True)
    dni = Column(String, unique=True, index=True)
    nombre_completo = Column(String)
    legajo = Column(String, unique=True, index=True)
    secretaria = Column(String, default="GOBIERNO")
    funcion = Column(String, default="ADMINISTRATIVA")
    sector_id = Column(Integer, ForeignKey("sectores.id"))
    activo = Column(Boolean, default=True)

    sector = relationship("Sector", back_populates="empleados")
    asistencias = relationship("Asistencia", back_populates="empleado")

class Asistencia(Base):
    __tablename__ = "asistencias"

    id = Column(Integer, primary_key=True, index=True)
    empleado_id = Column(Integer, ForeignKey("empleados.id"))
    fecha = Column(Date)
    hora_llegada = Column(String, nullable=True)
    hora_salida = Column(String, nullable=True)
    estado = Column(String, default="Presente")
    sincronizado = Column(Boolean, default=False)
    foto_llegada = Column(Text, nullable=True)
    foto_salida = Column(Text, nullable=True)

    empleado = relationship("Empleado", back_populates="asistencias")

class AuditoriaLog(Base):
    __tablename__ = "auditoria_logs"
    id = Column(Integer, primary_key=True, index=True)
    fecha_hora = Column(String)
    usuario = Column(String)
    nombre_operador = Column(String)
    accion = Column(String)
    detalle = Column(Text)