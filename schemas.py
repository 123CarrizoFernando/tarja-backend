from pydantic import BaseModel
from datetime import date

# ==========================================
#                SECTOR
# ==========================================
class SectorBase(BaseModel):
    nombre: str

class SectorCreate(SectorBase):
    pass

class Sector(SectorBase):
    id: int
    class Config:
        from_attributes = True

# ==========================================
#               ENCARGADO
# ==========================================
class EncargadoBase(BaseModel):
    usuario: str
    sector_id: int

class EncargadoCreate(EncargadoBase):
    password: str

class Encargado(EncargadoBase):
    id: int
    class Config:
        from_attributes = True

# ==========================================
#                EMPLEADO
# ==========================================
class EmpleadoBase(BaseModel):
    dni: str
    nombre_completo: str
    legajo: str
    sector_id: int

class EmpleadoCreate(EmpleadoBase):
    pass

class Empleado(EmpleadoBase):
    id: int
    class Config:
        from_attributes = True

# ==========================================
#               ASISTENCIA
# ==========================================
# ==========================================
#               ASISTENCIA
# ==========================================
class AsistenciaCreate(BaseModel):
    empleado_id: int
    fecha: date
    hora_llegada: str | None = None
    hora_salida: str | None = None

class Asistencia(AsistenciaCreate):
    id: int
    class Config:
        from_attributes = True