from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, HTMLResponse, StreamingResponse, FileResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import extract, func, text
from datetime import date, datetime, timedelta, timezone
from passlib.context import CryptContext
from jose import JWTError, jwt
from typing import List

import base64
import requests
import os
import io
import csv
import re
import calendar

from pydantic import BaseModel
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet

import models
import schemas
from database import engine, get_db

models.Base.metadata.create_all(bind=engine)

# Migración automática segura en Neon PostgreSQL
with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE asistencias ADD COLUMN IF NOT EXISTS foto_llegada TEXT"))
        conn.execute(text("ALTER TABLE asistencias ADD COLUMN IF NOT EXISTS foto_salida TEXT"))
        conn.execute(text("ALTER TABLE empleados ADD COLUMN IF NOT EXISTS secretaria TEXT DEFAULT 'GOBIERNO'"))
        conn.execute(text("ALTER TABLE empleados ADD COLUMN IF NOT EXISTS funcion TEXT DEFAULT 'ADMINISTRATIVA'"))
        conn.execute(text("ALTER TABLE encargados ADD COLUMN IF NOT EXISTS nombre_completo TEXT"))
        conn.execute(text("ALTER TABLE encargados ADD COLUMN IF NOT EXISTS activo BOOLEAN DEFAULT TRUE"))
        conn.commit()
    except Exception as e:
        print("Aviso migración:", e)

app = FastAPI(title="Tarja Backend API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CONFIGURACIÓN DE SEGURIDAD (JWT) ---
SECRET_KEY = "tu_super_clave_secreta_aqui"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

ZONA_ARG = timezone(timedelta(hours=-3))

def obtener_ahora_arg() -> datetime:
    return datetime.now(ZONA_ARG)

def limpiar_solo_numeros(valor: str | None) -> str:
    if not valor:
        return ""
    return re.sub(r'[^0-9]', '', str(valor))

def registrar_auditoria(db: Session, usuario_obj: models.Encargado, accion: str, detalle: str):
    ahora_str = obtener_ahora_arg().strftime("%d/%m/%Y %H:%M:%S")
    nombre_op = getattr(usuario_obj, "nombre_completo", None) or usuario_obj.usuario
    nuevo_log = models.AuditoriaLog(
        fecha_hora=ahora_str,
        usuario=usuario_obj.usuario,
        nombre_operador=nombre_op,
        accion=accion,
        detalle=detalle
    )
    db.add(nuevo_log)
    db.commit()

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta if expires_delta else timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def obtener_usuario_actual(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudieron validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    usuario = db.query(models.Encargado).filter(models.Encargado.usuario == username).first()
    if usuario is None:
        raise credentials_exception
    if getattr(usuario, "activo", True) is False:
        raise HTTPException(status_code=403, detail="Su cuenta de usuario ha sido desactivada por el Administrador.")
    return usuario

# =====================================================================
# LOS 3 NIVELES DE PERMISOS SEGÚN ROL
# =====================================================================
def obtener_permiso_reportes(usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    if usuario_actual.rol not in ["admin", "carga", "reportes", "rrhh"]:
        raise HTTPException(status_code=403, detail="Acceso denegado: No tiene permisos para visualizar reportes.")
    return usuario_actual

def obtener_permiso_carga(usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    if usuario_actual.rol not in ["admin", "carga", "rrhh"]:
        raise HTTPException(status_code=403, detail="Acceso denegado: Su cuenta es solo de visualización de reportes.")
    return usuario_actual

def obtener_admin_total(usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    if usuario_actual.rol not in ["admin"]:
        raise HTTPException(status_code=403, detail="Acceso denegado: Función exclusiva del Administrador Total.")
    return usuario_actual

# --- FUNCIÓN CALCULADORA DE TIEMPOS Y JORNADA DE 6 HORAS ---
def calcular_tiempos_jornada(hora_llegada_str: str | None, hora_salida_str: str | None = None, fecha_registro: date | None = None, horas_exigidas: int = 6):
    if not hora_llegada_str:
        return {
            "trabajado_str": "00h 00m",
            "salida_estimada": "--:--",
            "minutos_totales": 0,
            "en_curso": False,
            "cumplio_jornada": False
        }
    try:
        ahora = obtener_ahora_arg()
        h_in, m_in = map(int, hora_llegada_str.split(":"))
        dt_ingreso = ahora.replace(hour=h_in, minute=m_in, second=0, microsecond=0)
        
        dt_salida_estimada = dt_ingreso + timedelta(hours=horas_exigidas)
        salida_estimada_str = dt_salida_estimada.strftime("%H:%M")
        
        en_curso = False
        if hora_salida_str:
            h_out, m_out = map(int, hora_salida_str.split(":"))
            dt_fin = ahora.replace(hour=h_out, minute=m_out, second=0, microsecond=0)
        else:
            if fecha_registro is None or fecha_registro == ahora.date():
                dt_fin = ahora
                en_curso = True
            else:
                return {
                    "trabajado_str": "Sin salida",
                    "salida_estimada": salida_estimada_str,
                    "minutos_totales": 0,
                    "en_curso": False,
                    "cumplio_jornada": False
                }

        diferencia = dt_fin - dt_ingreso
        segundos = max(0, int(diferencia.total_seconds()))
        minutos_totales = segundos // 60
        horas = minutos_totales // 60
        minutos = minutos_totales % 60

        return {
            "trabajado_str": f"{horas:02d}h {minutos:02d}m",
            "salida_estimada": salida_estimada_str,
            "minutos_totales": minutos_totales,
            "en_curso": en_curso,
            "cumplio_jornada": minutos_totales >= (horas_exigidas * 60)
        }
    except Exception:
        return {
            "trabajado_str": "00h 00m",
            "salida_estimada": "--:--",
            "minutos_totales": 0,
            "en_curso": False,
            "cumplio_jornada": False
        }

# ==========================================
# ESQUEMAS
# ==========================================
class ItemCatalogoNuevo(BaseModel):
    nombre: str

class FeriadoNuevo(BaseModel):
    fecha: date
    descripcion: str

class AsistenciaManualAdmin(BaseModel):
    dni: str
    fecha: date
    estado: str = "Presente" # "Presente", "Licencia", "Falta"
    hora_llegada: str | None = None
    hora_salida: str | None = None

class CambioPasswordAdmin(BaseModel):
    nueva_password: str

class EmpleadoNuevo(BaseModel):
    dni: str
    nombre_completo: str
    legajo: str
    sector_id: int
    secretaria: str = "GOBIERNO"
    funcion: str = "ADMINISTRATIVA"

class EncargadoNuevoAdmin(BaseModel):
    usuario: str
    nombre_completo: str | None = None
    password: str
    sector_id: int | None = None
    rol: str = "encargado"

class FichajeKioscoRequest(BaseModel):
    dni: str
    tipo: str
    foto_base64: str | None = None

class ImportacionMasivaTexto(BaseModel):
    csv_texto: str

# ==========================================
# LOGIN E INICIO DE SESIÓN
# ==========================================
@app.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    usuario = db.query(models.Encargado).filter(models.Encargado.usuario == form_data.username).first()
    if not usuario or not verify_password(form_data.password, usuario.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if getattr(usuario, "activo", True) is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Esta cuenta de usuario se encuentra bloqueada por el Administrador.",
        )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": usuario.usuario}, expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token, 
        "token_type": "bearer", 
        "sector_id": usuario.sector_id,
        "rol": usuario.rol,
        "usuario": usuario.usuario,
        "nombre_completo": getattr(usuario, "nombre_completo", None) or usuario.usuario
    }

# ==========================================
# CATÁLOGOS: SECTORES, SECRETARÍAS, FUNCIONES Y FERIADOS
# ==========================================
@app.get("/admin/sectores")
def ver_sectores_admin(db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_reportes)):
    return db.query(models.Sector).order_by(models.Sector.nombre).all()

@app.post("/admin/sectores")
def crear_sector_admin(sector: ItemCatalogoNuevo, db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_carga)):
    nom = sector.nombre.strip()
    existe = db.query(models.Sector).filter(func.lower(models.Sector.nombre) == nom.lower()).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ese sector ya existe.")
    nuevo_sector = models.Sector(nombre=nom)
    db.add(nuevo_sector)
    db.commit()
    db.refresh(nuevo_sector)
    registrar_auditoria(db, user, "ALTA SECTOR", f"Creó el sector '{nom}'")
    return {"mensaje": f"Sector '{nom}' creado con éxito", "sector": nuevo_sector}

@app.get("/admin/secretarias")
def ver_secretarias_admin(db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_reportes)):
    lista = db.query(models.Secretaria).order_by(models.Secretaria.nombre).all()
    if not lista:
        por_defecto = models.Secretaria(nombre="GOBIERNO")
        db.add(por_defecto)
        db.commit()
        db.refresh(por_defecto)
        return [por_defecto]
    return lista

@app.post("/admin/secretarias")
def crear_secretaria_admin(sec: ItemCatalogoNuevo, db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_carga)):
    nom = sec.nombre.strip().upper()
    existe = db.query(models.Secretaria).filter(func.upper(models.Secretaria.nombre) == nom).first()
    if existe:
        raise HTTPException(status_code=400, detail="Esa Secretaría ya existe.")
    nueva = models.Secretaria(nombre=nom)
    db.add(nueva)
    db.commit()
    db.refresh(nueva)
    registrar_auditoria(db, user, "ALTA SECRETARÍA", f"Creó la Secretaría '{nom}'")
    return {"mensaje": f"Secretaría '{nom}' creada con éxito", "secretaria": nueva}

@app.get("/admin/funciones")
def ver_funciones_admin(db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_reportes)):
    lista = db.query(models.Funcion).order_by(models.Funcion.nombre).all()
    if not lista:
        por_defecto = models.Funcion(nombre="ADMINISTRATIVA")
        db.add(por_defecto)
        db.commit()
        db.refresh(por_defecto)
        return [por_defecto]
    return lista

@app.post("/admin/funciones")
def crear_funcion_admin(fn: ItemCatalogoNuevo, db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_carga)):
    nom = fn.nombre.strip().upper()
    existe = db.query(models.Funcion).filter(func.upper(models.Funcion.nombre) == nom).first()
    if existe:
        raise HTTPException(status_code=400, detail="Esa Función ya existe.")
    nueva = models.Funcion(nombre=nom)
    db.add(nueva)
    db.commit()
    db.refresh(nueva)
    registrar_auditoria(db, user, "ALTA FUNCIÓN", f"Creó la Función '{nom}'")
    return {"mensaje": f"Función '{nom}' creada con éxito", "funcion": nueva}

@app.get("/admin/feriados")
def ver_feriados_admin(db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_reportes)):
    feriados = db.query(models.Feriado).order_by(models.Feriado.fecha.desc()).all()
    return [{
        "id": f.id,
        "fecha": f.fecha.strftime("%Y-%m-%d"),
        "fecha_str": f.fecha.strftime("%d/%m/%Y"),
        "descripcion": f.descripcion
    } for f in feriados]

@app.post("/admin/feriados")
def crear_feriado_admin(datos: FeriadoNuevo, db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_carga)):
    existe = db.query(models.Feriado).filter(models.Feriado.fecha == datos.fecha).first()
    if existe:
        raise HTTPException(status_code=400, detail="Esa fecha ya está registrada como Feriado / Asueto.")
    nuevo = models.Feriado(fecha=datos.fecha, descripcion=datos.descripcion.strip().upper())
    db.add(nuevo)
    db.commit()
    registrar_auditoria(db, user, "ALTA FERIADO", f"Declaró Feriado/Asueto el día {datos.fecha.strftime('%d/%m/%Y')}: {nuevo.descripcion}")
    return {"mensaje": f"Feriado {datos.fecha.strftime('%d/%m/%Y')} registrado con éxito."}

@app.delete("/admin/feriados/{feriado_id}")
def eliminar_feriado_admin(feriado_id: int, db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_carga)):
    fer = db.query(models.Feriado).filter(models.Feriado.id == feriado_id).first()
    if not fer:
        raise HTTPException(status_code=404, detail="Feriado no encontrado.")
    detalle = f"{fer.fecha.strftime('%d/%m/%Y')} ({fer.descripcion})"
    db.delete(fer)
    db.commit()
    registrar_auditoria(db, user, "BAJA FERIADO", f"Eliminó el Feriado {detalle}")
    return {"mensaje": "Feriado eliminado correctamente."}

# ==========================================
# CORRECCIÓN DE "OLVIDO DE SALIDA" Y CARGA DE LICENCIAS / FALTAS EN LA WEB
# ==========================================
@app.post("/admin/asistencias/manual")
def corregir_o_cargar_asistencia_web(datos: AsistenciaManualAdmin, db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_carga)):
    dni_limpio = limpiar_solo_numeros(datos.dni)
    empleado = db.query(models.Empleado).filter(models.Empleado.dni == dni_limpio).first()
    if not empleado:
        raise HTTPException(status_code=404, detail="No existe ningún agente con ese DNI.")

    asis = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id == empleado.id,
        models.Asistencia.fecha == datos.fecha
    ).first()

    llegada_limpia = datos.hora_llegada.strip() if datos.hora_llegada and datos.hora_llegada.strip() else None
    salida_limpia = datos.hora_salida.strip() if datos.hora_salida and datos.hora_salida.strip() else None

    if datos.estado in ["Falta", "Licencia"]:
        llegada_limpia = None
        salida_limpia = None

    if not asis:
        asis = models.Asistencia(
            empleado_id=empleado.id,
            fecha=datos.fecha,
            estado=datos.estado,
            hora_llegada=llegada_limpia,
            hora_salida=salida_limpia,
            sincronizado=True
        )
        db.add(asis)
        accion_log = "CARGA MANUAL ASISTENCIA/LICENCIA"
    else:
        asis.estado = datos.estado
        asis.hora_llegada = llegada_limpia
        asis.hora_salida = salida_limpia
        accion_log = "CORRECCIÓN HORARIO / ESTADO"

    db.commit()
    registrar_auditoria(
        db, user, accion_log,
        f"Agente: {empleado.nombre_completo} (DNI {dni_limpio}) | Fecha: {datos.fecha.strftime('%d/%m/%Y')} -> Estado: {datos.estado} | Entrada: {llegada_limpia or '-'} | Salida: {salida_limpia or '-'}"
    )
    return {"mensaje": f"Registro de {empleado.nombre_completo} del día {datos.fecha.strftime('%d/%m/%Y')} guardado correctamente."}

# ==========================================
# PADRÓN DE EMPLEADOS E IMPORTACIÓN MASIVA
# ==========================================
@app.get("/admin/empleados")
def ver_todos_los_empleados_admin(sector_id: int | None = None, db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_carga)):
    query = db.query(models.Empleado).filter(models.Empleado.activo == True)
    if sector_id:
        query = query.filter(models.Empleado.sector_id == sector_id)
    empleados = query.order_by(models.Empleado.nombre_completo).all()
    
    return [{
        "id": emp.id,
        "dni": emp.dni,
        "nombre_completo": emp.nombre_completo,
        "legajo": emp.legajo,
        "secretaria": getattr(emp, "secretaria", None) or "GOBIERNO",
        "funcion": getattr(emp, "funcion", None) or "ADMINISTRATIVA",
        "sector_id": emp.sector_id,
        "sector_nombre": emp.sector.nombre if emp.sector else "Sin sector"
    } for emp in empleados]

@app.post("/admin/empleados")
def crear_empleado_admin(empleado: EmpleadoNuevo, db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_carga)):
    dni_limpio = limpiar_solo_numeros(empleado.dni)
    cuil_limpio = limpiar_solo_numeros(empleado.legajo)

    existe = db.query(models.Empleado).filter(
        (models.Empleado.dni == dni_limpio) | (models.Empleado.legajo == cuil_limpio)
    ).first()
    if existe:
        raise HTTPException(status_code=400, detail="El DNI o CUIL/Legajo ya está registrado en la base de datos.")

    nuevo_empleado = models.Empleado(
        dni=dni_limpio,
        nombre_completo=empleado.nombre_completo.strip().upper(),
        legajo=cuil_limpio,
        secretaria=empleado.secretaria.strip().upper(),
        funcion=empleado.funcion.strip().upper(),
        sector_id=empleado.sector_id,
        activo=True
    )
    db.add(nuevo_empleado)
    db.commit()

    registrar_auditoria(
        db, user, "ALTA EMPLEADO",
        f"Registró a {nuevo_empleado.nombre_completo} (DNI: {dni_limpio}, CUIL: {cuil_limpio}, Sec: {nuevo_empleado.secretaria}, Func: {nuevo_empleado.funcion})"
    )
    return {"mensaje": f"Empleado {nuevo_empleado.nombre_completo} (DNI: {dni_limpio}) registrado con éxito"}

@app.put("/admin/empleados/{empleado_id}")
def editar_empleado_admin(empleado_id: int, datos: EmpleadoNuevo, db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_carga)):
    empleado = db.query(models.Empleado).filter(models.Empleado.id == empleado_id).first()
    if not empleado:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    
    dni_limpio = limpiar_solo_numeros(datos.dni)
    cuil_limpio = limpiar_solo_numeros(datos.legajo)

    duplicado = db.query(models.Empleado).filter(
        models.Empleado.id != empleado_id,
        (models.Empleado.dni == dni_limpio) | (models.Empleado.legajo == cuil_limpio)
    ).first()
    if duplicado:
        raise HTTPException(status_code=400, detail="El DNI o CUIL/Legajo ya pertenece a otro empleado.")
    
    datos_antes = f"{empleado.nombre_completo} (DNI:{empleado.dni}, CUIL:{empleado.legajo}, Sec:{empleado.secretaria}, Func:{empleado.funcion})"

    empleado.nombre_completo = datos.nombre_completo.strip().upper()
    empleado.dni = dni_limpio
    empleado.legajo = cuil_limpio
    empleado.secretaria = datos.secretaria.strip().upper()
    empleado.funcion = datos.funcion.strip().upper()
    empleado.sector_id = datos.sector_id
    db.commit()

    registrar_auditoria(
        db, user, "EDICIÓN EMPLEADO",
        f"Modificó ID #{empleado.id}. Antes: [{datos_antes}] -> Ahora: [{empleado.nombre_completo} (DNI:{dni_limpio}, CUIL:{cuil_limpio}, Sec:{empleado.secretaria}, Func:{empleado.funcion})]"
    )
    return {"mensaje": "Datos actualizados correctamente."}

@app.post("/admin/importar-masivo")
def importar_empleados_masivo(payload: ImportacionMasivaTexto, db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_carga)):
    lineas = [l.strip() for l in payload.csv_texto.splitlines() if l.strip()]
    if not lineas:
        raise HTTPException(status_code=400, detail="El archivo o texto está vacío.")

    creados = 0
    actualizados = 0

    for idx, linea in enumerate(lineas):
        sep = ';' if ';' in linea else ('\t' if '\t' in linea else ',')
        partes = [p.strip() for p in linea.split(sep)]
        if len(partes) < 2:
            continue

        primera_col = limpiar_solo_numeros(partes[0])
        if idx == 0 and not primera_col:
            continue

        if len(partes) >= 6 and limpiar_solo_numeros(partes[1]):
            cuil = limpiar_solo_numeros(partes[0])
            dni = limpiar_solo_numeros(partes[1])
            nombre = partes[2].strip().upper()
            secretaria = (partes[3].strip() or "GOBIERNO").upper()
            sector_nom = partes[4].strip() or "General"
            funcion = (partes[5].strip() or "ADMINISTRATIVA").upper()
        else:
            cuil = limpiar_solo_numeros(partes[0])
            dni = cuil[2:10] if len(cuil) == 11 else cuil
            nombre = partes[1].strip().upper()
            secretaria = (partes[2].strip() if len(partes) > 2 and partes[2].strip() else "GOBIERNO").upper()
            sector_nom = partes[3].strip() if len(partes) > 3 and partes[3].strip() else "General"
            funcion = (partes[4].strip() if len(partes) > 4 and partes[4].strip() else "ADMINISTRATIVA").upper()

        if not nombre or (not cuil and not dni):
            continue
        if not cuil:
            cuil = dni
        if not dni:
            dni = cuil[2:10] if len(cuil) == 11 else cuil

        sec_cat = db.query(models.Secretaria).filter(func.upper(models.Secretaria.nombre) == secretaria).first()
        if not sec_cat:
            db.add(models.Secretaria(nombre=secretaria))
            db.flush()

        fn_cat = db.query(models.Funcion).filter(func.upper(models.Funcion.nombre) == funcion).first()
        if not fn_cat:
            db.add(models.Funcion(nombre=funcion))
            db.flush()

        sector_obj = db.query(models.Sector).filter(func.lower(models.Sector.nombre) == sector_nom.lower()).first()
        if not sector_obj:
            sector_obj = models.Sector(nombre=sector_nom)
            db.add(sector_obj)
            db.flush()

        emp_existente = db.query(models.Empleado).filter(
            (models.Empleado.dni == dni) | (models.Empleado.legajo == cuil)
        ).first()

        if emp_existente:
            emp_existente.nombre_completo = nombre
            emp_existente.dni = dni
            emp_existente.legajo = cuil
            emp_existente.secretaria = secretaria
            emp_existente.funcion = funcion
            emp_existente.sector_id = sector_obj.id
            emp_existente.activo = True
            actualizados += 1
        else:
            db.add(models.Empleado(
                dni=dni,
                legajo=cuil,
                nombre_completo=nombre,
                secretaria=secretaria,
                funcion=funcion,
                sector_id=sector_obj.id,
                activo=True
            ))
            creados += 1

    db.commit()
    registrar_auditoria(
        db, user, "IMPORTACIÓN MASIVA",
        f"Sincronizó padrón masivo: {creados} agentes nuevos creados y {actualizados} actualizados."
    )
    return {
        "mensaje": f"Importación completada: {creados} agentes nuevos creados y {actualizados} actualizados."
    }

# ==========================================
# GESTIÓN DE USUARIOS, CAMBIO DE CLAVE, BLOQUEO Y AUDITORÍA (EXCLUSIVO ADMIN TOTAL)
# ==========================================
@app.get("/admin/encargados")
def ver_encargados_admin(db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_total)):
    encargados = db.query(models.Encargado).order_by(models.Encargado.id).all()
    return [{
        "id": enc.id,
        "usuario": enc.usuario,
        "nombre_completo": getattr(enc, "nombre_completo", None) or enc.usuario,
        "rol": enc.rol,
        "activo": getattr(enc, "activo", True) if getattr(enc, "activo", True) is not None else True,
        "sector_nombre": enc.sector.nombre if enc.sector else "Global (Todos los sectores)"
    } for enc in encargados]

@app.post("/admin/encargados")
def crear_encargado_admin(datos: EncargadoNuevoAdmin, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_total)):
    existe = db.query(models.Encargado).filter(models.Encargado.usuario == datos.usuario.strip()).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ese nombre de usuario ya existe.")
    
    roles_validos = ["admin", "carga", "reportes", "encargado"]
    rol_elegido = datos.rol if datos.rol in roles_validos else "encargado"
    nombre_op = (datos.nombre_completo or datos.usuario).strip().upper()

    nuevo_encargado = models.Encargado(
        usuario=datos.usuario.strip(),
        nombre_completo=nombre_op,
        password_hash=get_password_hash(datos.password),
        sector_id=datos.sector_id if rol_elegido == "encargado" else None,
        rol=rol_elegido,
        activo=True
    )
    db.add(nuevo_encargado)
    db.commit()

    registrar_auditoria(
        db, admin, "ALTA USUARIO SISTEMA",
        f"Dio de alta al usuario '{nuevo_encargado.usuario}' ({nombre_op}) con rol '{rol_elegido.upper()}'"
    )
    return {"mensaje": f"Usuario {nuevo_encargado.usuario} ({nombre_op}) creado con éxito."}

@app.put("/admin/encargados/{enc_id}/password")
def cambiar_password_usuario(enc_id: int, datos: CambioPasswordAdmin, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_total)):
    enc = db.query(models.Encargado).filter(models.Encargado.id == enc_id).first()
    if not enc:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if len(datos.nueva_password.strip()) < 4:
        raise HTTPException(status_code=400, detail="La contraseña debe tener al menos 4 caracteres.")
    enc.password_hash = get_password_hash(datos.nueva_password.strip())
    db.commit()
    registrar_auditoria(db, admin, "CAMBIO CLAVE USUARIO", f"Blanqueó/actualizó la contraseña del usuario '{enc.usuario}'")
    return {"mensaje": f"Contraseña de '{enc.usuario}' actualizada con éxito."}

@app.put("/admin/encargados/{enc_id}/estado")
def cambiar_estado_usuario(enc_id: int, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_total)):
    enc = db.query(models.Encargado).filter(models.Encargado.id == enc_id).first()
    if not enc:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if enc.id == admin.id:
        raise HTTPException(status_code=400, detail="No puede bloquear su propia cuenta de Administrador Total.")
    estado_actual = getattr(enc, "activo", True)
    nuevo_estado = not (True if estado_actual is None else estado_actual)
    enc.activo = nuevo_estado
    db.commit()
    accion_txt = "ACTIVÓ USUARIO" if nuevo_estado else "BLOQUEÓ USUARIO"
    registrar_auditoria(db, admin, accion_txt, f"Cambió el estado del usuario '{enc.usuario}' a {'ACTIVO' if nuevo_estado else 'BLOQUEADO'}")
    return {"mensaje": f"Usuario '{enc.usuario}' {'activado' if nuevo_estado else 'bloqueado'} correctamente."}

@app.get("/admin/auditoria")
def obtener_historial_auditoria(limite: int = 200, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_total)):
    logs = db.query(models.AuditoriaLog).order_by(models.AuditoriaLog.id.desc()).limit(limite).all()
    return [{
        "id": l.id,
        "fecha_hora": l.fecha_hora,
        "usuario": l.usuario,
        "nombre_operador": l.nombre_operador,
        "accion": l.accion,
        "detalle": l.detalle
    } for l in logs]

# ==========================================
# CONSULTA DE ASISTENCIAS Y REPORTES
# ==========================================
@app.get("/admin/asistencias/{fecha_solicitada}")
def obtener_todas_asistencias(
    fecha_solicitada: date,
    hasta: date | None = None,
    sector_id: int | None = None,
    dni: str | None = None,
    db: Session = Depends(get_db),
    user: models.Encargado = Depends(obtener_permiso_reportes)
):
    fecha_fin = hasta if hasta else fecha_solicitada
    query = db.query(models.Asistencia).join(models.Empleado).filter(
        models.Asistencia.fecha >= fecha_solicitada,
        models.Asistencia.fecha <= fecha_fin
    )
    if sector_id:
        query = query.filter(models.Empleado.sector_id == sector_id)
    if dni and dni.strip():
        query = query.filter(models.Empleado.dni.contains(dni.strip()))
        
    asistencias = query.order_by(models.Asistencia.fecha.desc(), models.Empleado.nombre_completo).all()
    
    resultado = []
    for asis in asistencias:
        estado_real = asis.estado if asis.estado else "Presente"
        calc = calcular_tiempos_jornada(asis.hora_llegada, asis.hora_salida, asis.fecha)
        
        resultado.append({
            "empleado_id": asis.empleado_id,
            "fecha": asis.fecha.strftime("%d/%m/%Y"),
            "fecha_iso": asis.fecha.strftime("%Y-%m-%d"),
            "dni": asis.empleado.dni,
            "nombre": asis.empleado.nombre_completo,
            "legajo": limpiar_solo_numeros(asis.empleado.legajo),
            "sector_id": asis.empleado.sector_id,
            "sector": asis.empleado.sector.nombre if asis.empleado.sector else "Sin sector",
            "hora_llegada": asis.hora_llegada,
            "hora_salida": asis.hora_salida,
            "salida_estimada": calc["salida_estimada"],
            "tiempo_trabajado": calc["trabajado_str"],
            "en_curso": calc["en_curso"] and (estado_real == "Presente"),
            "cumplio_jornada": calc["cumplio_jornada"],
            "estado": estado_real,
            "foto_llegada": asis.foto_llegada,
            "foto_salida": asis.foto_salida
        })
    return resultado

@app.get("/admin/dashboard")
def obtener_dashboard_admin(db: Session = Depends(get_db), user: models.Encargado = Depends(obtener_permiso_reportes)):
    hoy = obtener_ahora_arg().date()
    total_empleados = db.query(models.Empleado).filter(models.Empleado.activo == True).count()
    asistencias_hoy = db.query(models.Asistencia).filter(models.Asistencia.fecha == hoy).all()
    
    presentes, faltas, licencias = 0, 0, 0
    for asis in asistencias_hoy:
        estado_real = asis.estado if asis.estado else "Presente"
        if estado_real == "Falta": faltas += 1
        elif estado_real == "Licencia": licencias += 1
        else: presentes += 1
            
    sin_marcar = max(0, total_empleados - (presentes + faltas + licencias))
    primer_dia_mes = hoy.replace(day=1)
    
    ranking = db.query(
        models.Sector.nombre,
        func.count(models.Asistencia.id).label('total_faltas')
    ).select_from(models.Asistencia).join(models.Empleado).join(models.Sector).filter(
        models.Asistencia.estado == 'Falta',
        models.Asistencia.fecha >= primer_dia_mes,
        models.Asistencia.fecha <= hoy
    ).group_by(models.Sector.nombre).order_by(func.count(models.Asistencia.id).desc()).limit(3).all()
    
    return {
        "hoy": {"total": total_empleados, "presentes": presentes, "faltas": faltas, "licencias": licencias, "sin_marcar": sin_marcar},
        "ranking_mes": [{"sector": r[0], "faltas": r[1]} for r in ranking]
    }

@app.get("/logo.png")
def obtener_logo_muni():
    if os.path.exists("logo_muni_web.png"):
        return FileResponse("logo_muni_web.png")
    if os.path.exists("logo_muni.png"):
        return FileResponse("logo_muni.png")
    raise HTTPException(status_code=404, detail="Logo no encontrado")

@app.get("/admin/reporte_pdf")
def descargar_pdf_admin(
    fecha: date | None = None,
    hasta: date | None = None,
    sector_id: int | None = None,
    dni: str | None = None,
    db: Session = Depends(get_db),
    user: models.Encargado = Depends(obtener_permiso_reportes)
):
    fecha_desde = fecha if fecha else obtener_ahora_arg().date()
    fecha_hasta = hasta if hasta else fecha_desde
    
    query = db.query(models.Asistencia).join(models.Empleado).outerjoin(models.Sector).filter(
        models.Asistencia.fecha >= fecha_desde,
        models.Asistencia.fecha <= fecha_hasta
    )
    nombre_sector_filtro = "Todos los Sectores"
    
    if sector_id:
        query = query.filter(models.Empleado.sector_id == sector_id)
        sec_obj = db.query(models.Sector).filter(models.Sector.id == sector_id).first()
        if sec_obj:
            nombre_sector_filtro = sec_obj.nombre
    if dni and dni.strip():
        query = query.filter(models.Empleado.dni.contains(dni.strip()))

    asistencias = query.order_by(
        models.Sector.nombre.asc(),
        models.Asistencia.fecha.desc(),
        models.Empleado.nombre_completo.asc()
    ).all()

    if not asistencias:
        raise HTTPException(status_code=404, detail="No hay asistencias registradas para ese filtro.")

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4, topMargin=30, bottomMargin=30)
    elementos = []
    estilos = getSampleStyleSheet()

    if os.path.exists("logo_muni.png"):
        elementos.append(Image("logo_muni.png", width=480, height=110))
        elementos.append(Spacer(1, 10))

    rango_txt = fecha_desde.strftime('%d/%m/%Y') if fecha_desde == fecha_hasta else f"del {fecha_desde.strftime('%d/%m/%Y')} al {fecha_hasta.strftime('%d/%m/%Y')}"
    titulo = Paragraph(f"<b>Reporte General de Tarja Municipal</b><br/><font size=12 color='#15803d'>{nombre_sector_filtro} ({rango_txt})</font>", estilos['Title'])
    elementos.append(titulo)
    elementos.append(Spacer(1, 15))

    asistencias_por_sector = {}
    for asis in asistencias:
        emp = asis.empleado
        sec_nom = emp.sector.nombre if emp.sector else "Sin Sector Asignado"
        if sec_nom not in asistencias_por_sector:
            asistencias_por_sector[sec_nom] = []
        asistencias_por_sector[sec_nom].append(asis)

    for sec_nombre, lista_asis in asistencias_por_sector.items():
        encabezado_sector = Paragraph(
            f"<b>SECTOR: {sec_nombre.upper()}</b> <font size=9 color='#475569'>({len(lista_asis)} registros)</font>",
            estilos['Heading3']
        )
        elementos.append(encabezado_sector)
        elementos.append(Spacer(1, 4))

        datos_tabla = [["Fecha", "CUIL / Legajo", "DNI", "Empleado", "Entrada", "Salida", "Horas"]]
        for asis in lista_asis:
            emp = asis.empleado
            fecha_fila = asis.fecha.strftime("%d/%m/%Y")
            if asis.estado and asis.estado != "Presente":
                llegada, salida, trabajado = asis.estado, asis.estado, "00h 00m"
            else:
                llegada = asis.hora_llegada or "-"
                salida = asis.hora_salida or "En curso"
                calc = calcular_tiempos_jornada(asis.hora_llegada, asis.hora_salida, asis.fecha)
                trabajado = calc["trabajado_str"]
            datos_tabla.append([
                fecha_fila,
                limpiar_solo_numeros(emp.legajo),
                limpiar_solo_numeros(emp.dni) or "-",
                emp.nombre_completo,
                llegada,
                salida,
                trabajado
            ])

        tabla = Table(datos_tabla, colWidths=[58, 65, 55, 160, 50, 50, 58])
        tabla.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#15803d')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8.5),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, 0), 8),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8fafc')),
            ('GRID', (0, 0), (-1, -1), 0.7, colors.HexColor('#cbd5e1'))
        ]))
        elementos.append(tabla)
        elementos.append(Spacer(1, 18))

    pdf.build(elementos)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    nombre_archivo = f"Tarja_{nombre_sector_filtro.replace(' ', '_')}_{fecha_desde}_al_{fecha_hasta}.pdf"
    return Response(content=pdf_bytes, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"})

# ==========================================
# RUTAS DE ENCARGADOS (APP MÓVIL)
# ==========================================
@app.get("/empleados")
def obtener_empleados_sector(db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    if usuario_actual.rol in ["admin", "carga", "reportes", "rrhh"] and not usuario_actual.sector_id:
        return db.query(models.Empleado).filter(models.Empleado.activo == True).all()
    return db.query(models.Empleado).filter(
        models.Empleado.sector_id == usuario_actual.sector_id,
        models.Empleado.activo == True
    ).all()

@app.post("/asistencias/sincronizar")
def sincronizar_asistencias(asistencias: List[schemas.AsistenciaCreate], db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    for asis in asistencias:
        registro_existente = db.query(models.Asistencia).filter(
            models.Asistencia.empleado_id == asis.empleado_id,
            models.Asistencia.fecha == asis.fecha
        ).first()

        if registro_existente:
            if asis.hora_llegada: registro_existente.hora_llegada = asis.hora_llegada
            if asis.hora_salida: registro_existente.hora_salida = asis.hora_salida
        else:
            db.add(models.Asistencia(
                empleado_id=asis.empleado_id,
                fecha=asis.fecha,
                hora_llegada=asis.hora_llegada,
                hora_salida=asis.hora_salida,
                estado="Presente"
            ))
    db.commit()
    registrar_auditoria(db, usuario_actual, "SINCRONIZACIÓN APP", f"Sincronizó {len(asistencias)} asistencias desde el celular.")
    return {"mensaje": f"Se sincronizaron {len(asistencias)} registros con éxito."}

@app.get("/asistencias/hoy")
def obtener_asistencias_hoy(db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    hoy = obtener_ahora_arg().date()
    empleados_sector = db.query(models.Empleado).filter(models.Empleado.sector_id == usuario_actual.sector_id).all()
    ids_empleados = [emp.id for emp in empleados_sector]
    return db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id.in_(ids_empleados),
        models.Asistencia.fecha == hoy
    ).all()

@app.put("/asistencias/{empleado_id}/estado")
def registrar_estado_especial(empleado_id: int, estado: str, db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    hoy = obtener_ahora_arg().date()
    emp = db.query(models.Empleado).filter(models.Empleado.id == empleado_id).first()
    asistencia = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id == empleado_id,
        models.Asistencia.fecha == hoy
    ).first()

    if not asistencia:
        db.add(models.Asistencia(empleado_id=empleado_id, fecha=hoy, estado=estado))
    else:
        asistencia.estado = estado
        if estado != "Presente":
            asistencia.hora_llegada = None
            asistencia.hora_salida = None
    db.commit()
    nom_emp = emp.nombre_completo if emp else f"ID {empleado_id}"
    registrar_auditoria(db, usuario_actual, "CAMBIO ESTADO ASISTENCIA", f"Marcó '{estado}' a {nom_emp} en fecha {hoy}")
    return {"mensaje": f"Estado actualizado a {estado}."}

@app.get("/empleados/{empleado_id}/historial")
def obtener_historial_empleado(empleado_id: int, fecha_inicio: date, fecha_fin: date, db: Session = Depends(get_db)):
    empleado = db.query(models.Empleado).filter(models.Empleado.id == empleado_id).first()
    if not empleado:
        raise HTTPException(status_code=404, detail="Empleado no existe")

    asistencias = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id == empleado_id,
        models.Asistencia.fecha >= fecha_inicio,
        models.Asistencia.fecha <= fecha_fin
    ).order_by(models.Asistencia.fecha).all()

    minutos_totales = 0
    detalle = []
    for asis in asistencias:
        estado_real = asis.estado if asis.estado else "Presente"
        if estado_real == "Presente" and asis.hora_llegada:
            calc = calcular_tiempos_jornada(asis.hora_llegada, asis.hora_salida, asis.fecha)
            minutos_totales += calc["minutos_totales"]
            detalle.append({
                "fecha": asis.fecha,
                "llegada": asis.hora_llegada,
                "salida": asis.hora_salida or "En curso",
                "horas_trabajadas": calc["trabajado_str"],
                "estado": estado_real
            })
        else:
            detalle.append({
                "fecha": asis.fecha, "llegada": "-", "salida": "-",
                "horas_trabajadas": "00h 00m", "estado": estado_real
            })

    h_tot = minutos_totales // 60
    m_tot = minutos_totales % 60
    return {
        "empleado": empleado.nombre_completo,
        "legajo": limpiar_solo_numeros(empleado.legajo),
        "total_horas": f"{h_tot}h {m_tot}m",
        "detalle": detalle
    }

# =====================================================================
# KIOSCO PÚBLICO POR DNI CON CAPTURA DE FOTO ANTI-TRAMPA
# =====================================================================
@app.post("/kiosco/fichar")
def fichar_por_dni(datos: FichajeKioscoRequest, db: Session = Depends(get_db)):
    dni_limpio = limpiar_solo_numeros(datos.dni)
    tipo = datos.tipo.lower().strip()

    empleado = db.query(models.Empleado).filter(
        models.Empleado.dni == dni_limpio,
        models.Empleado.activo == True
    ).first()

    if not empleado:
        raise HTTPException(status_code=404, detail="No se encontró ningún empleado registrado con ese DNI.")

    ahora = obtener_ahora_arg()
    hoy = ahora.date()
    hora_actual = ahora.strftime("%H:%M")

    asistencia = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id == empleado.id,
        models.Asistencia.fecha == hoy
    ).first()

    if tipo == "consulta":
        primer_dia_mes = hoy.replace(day=1)
        asis_mes = db.query(models.Asistencia).filter(
            models.Asistencia.empleado_id == empleado.id,
            models.Asistencia.fecha >= primer_dia_mes,
            models.Asistencia.fecha <= hoy
        ).all()
        
        min_mes = 0
        for a in asis_mes:
            if (a.estado or "Presente") == "Presente" and a.hora_llegada:
                c = calcular_tiempos_jornada(a.hora_llegada, a.hora_salida, a.fecha)
                min_mes += c["minutos_totales"]
        
        acumulado_mes_str = f"{min_mes // 60} hs {min_mes % 60} min"

        if not asistencia or not asistencia.hora_llegada:
            return {
                "empleado": empleado.nombre_completo,
                "dni": empleado.dni,
                "legajo": limpiar_solo_numeros(empleado.legajo),
                "sector": empleado.sector.nombre if empleado.sector else "Sin sector",
                "mensaje": "Hoy todavía no registraste horario de entrada.",
                "submensaje": f"Acumulado trabajado en este mes: {acumulado_mes_str}"
            }
        
        calc_hoy = calcular_tiempos_jornada(asistencia.hora_llegada, asistencia.hora_salida, hoy)
        estado_txt = "En curso (Trabajando)" if calc_hoy["en_curso"] else f"Finalizado (Salió {asistencia.hora_salida})"
        return {
            "empleado": empleado.nombre_completo,
            "dni": empleado.dni,
            "legajo": limpiar_solo_numeros(empleado.legajo),
            "sector": empleado.sector.nombre if empleado.sector else "Sin sector",
            "mensaje": f"⏱️ Hoy llevás trabajado: {calc_hoy['trabajado_str']} ({estado_txt})",
            "submensaje": f"Entrada hoy: {asistencia.hora_llegada} hs | Salida p/ cumplir 6hs: {calc_hoy['salida_estimada']} hs | Total del Mes: {acumulado_mes_str}"
        }

    if tipo == "entrada":
        if asistencia and asistencia.estado in ["Falta", "Licencia"]:
            raise HTTPException(status_code=400, detail=f"Usted figura hoy con estado: {asistencia.estado}. Consulte con Personal.")
        if asistencia and asistencia.hora_llegada:
            calc = calcular_tiempos_jornada(asistencia.hora_llegada, None, hoy)
            raise HTTPException(
                status_code=400,
                detail=f"Ya registraste tu entrada a las {asistencia.hora_llegada} hs (Llevás {calc['trabajado_str']}). Debés salir a las {calc['salida_estimada']} hs."
            )
        if not asistencia:
            asistencia = models.Asistencia(
                empleado_id=empleado.id,
                fecha=hoy,
                hora_llegada=hora_actual,
                estado="Presente",
                sincronizado=True,
                foto_llegada=datos.foto_base64
            )
            db.add(asistencia)
        else:
            asistencia.hora_llegada = hora_actual
            asistencia.estado = "Presente"
            if datos.foto_base64:
                asistencia.foto_llegada = datos.foto_base64

        db.commit()
        calc = calcular_tiempos_jornada(hora_actual, None, hoy)
        aviso_foto = "📸 Fotografía de ingreso registrada correctamente." if datos.foto_base64 else "⚠️ Registro sin cámara."
        return {
            "empleado": empleado.nombre_completo,
            "sector": empleado.sector.nombre if empleado.sector else "Sin sector",
            "mensaje": f"¡Entrada registrada a las {hora_actual} hs!",
            "submensaje": f"Para completar tus 6 horas de trabajo, tu horario de salida hoy es a las {calc['salida_estimada']} hs. ({aviso_foto})"
        }

    elif tipo == "salida":
        if not asistencia or not asistencia.hora_llegada:
            raise HTTPException(status_code=400, detail="No tenés una entrada registrada hoy. Primero debés marcar entrada.")
        if asistencia.hora_salida:
            calc = calcular_tiempos_jornada(asistencia.hora_llegada, asistencia.hora_salida, hoy)
            raise HTTPException(status_code=400, detail=f"Ya registraste tu salida hoy a las {asistencia.hora_salida} hs (Total: {calc['trabajado_str']}).")

        asistencia.hora_salida = hora_actual
        if datos.foto_base64:
            asistencia.foto_salida = datos.foto_base64
        db.commit()

        calc = calcular_tiempos_jornada(asistencia.hora_llegada, hora_actual, hoy)
        estado_jornada = "✅ ¡Jornada de 6 horas completada!" if calc["cumplio_jornada"] else f"⚠️ Atención: Salida antes de las {calc['salida_estimada']} hs (menos de 6 hs)."
        return {
            "empleado": empleado.nombre_completo,
            "sector": empleado.sector.nombre if empleado.sector else "Sin sector",
            "mensaje": f"¡Salida registrada a las {hora_actual} hs!",
            "submensaje": f"Tiempo total trabajado hoy: {calc['trabajado_str']}. {estado_jornada}"
        }
    else:
        raise HTTPException(status_code=400, detail="Acción inválida.")

# =====================================================================
# EXCEL 1: REPORTE DIARIO DE ASISTENCIAS
# =====================================================================
@app.get("/rrhh/exportar-diario")
def exportar_excel_diario(
    desde: date,
    hasta: date,
    sector_id: int | None = None,
    dni: str | None = None,
    db: Session = Depends(get_db),
    user: models.Encargado = Depends(obtener_permiso_reportes)
):
    query = db.query(models.Asistencia).join(models.Empleado).filter(
        models.Asistencia.fecha >= desde,
        models.Asistencia.fecha <= hasta
    )
    if sector_id:
        query = query.filter(models.Empleado.sector_id == sector_id)
    if dni and dni.strip():
        query = query.filter(models.Empleado.dni.contains(dni.strip()))

    asistencias = query.order_by(
        models.Asistencia.fecha.desc(),
        models.Empleado.nombre_completo.asc()
    ).all()

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')

    writer.writerow([
        "Fecha",
        "Legajo",
        "DNI",
        "Apellido y Nombre",
        "Estado",
        "Hora Entrada",
        "Hora Salida Real",
        "Horas Trabajadas",
        "Horas Restadas del Dia"
    ])

    for a in asistencias:
        emp = a.empleado
        estado_real = a.estado if a.estado else "Presente"

        if estado_real == "Presente" and a.hora_llegada:
            calc = calcular_tiempos_jornada(a.hora_llegada, a.hora_salida, a.fecha)
            min_trabajados = calc["minutos_totales"]
            trabajado_str = calc["trabajado_str"]
        else:
            min_trabajados = 0
            trabajado_str = "00h 00m"

        if estado_real == "Licencia":
            min_restados = 0
        else:
            min_restados = max(0, 360 - min_trabajados)

        h_rest = min_restados // 60
        m_rest = min_restados % 60
        restadas_str = f"{h_rest:02d}h {m_rest:02d}m"

        salida_real_txt = a.hora_salida if a.hora_salida else ""

        writer.writerow([
            a.fecha.strftime("%d/%m/%Y"),
            limpiar_solo_numeros(emp.legajo),
            limpiar_solo_numeros(emp.dni),
            (emp.nombre_completo or "").upper(),
            estado_real,
            a.hora_llegada or "",
            salida_real_txt,
            trabajado_str,
            restadas_str
        ])

    output.seek(0)
    filename = f"Reporte_Asistencia_{desde}_al_{hasta}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

# =====================================================================
# EXCEL 2: REPORTE MENSUAL PARA HACIENDA (DESCONTANDO FERIADOS)
# =====================================================================
@app.get("/rrhh/exportar-mensual-hacienda")
def exportar_mensual_hacienda(
    mes: int | None = None,
    anio: int | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    sector_id: int | None = None,
    db: Session = Depends(get_db),
    user: models.Encargado = Depends(obtener_permiso_reportes)
):
    ahora = obtener_ahora_arg().date()
    
    if desde:
        anio_calc = desde.year
        mes_calc = desde.month
    else:
        anio_calc = anio if anio else ahora.year
        mes_calc = mes if mes else ahora.month

    _, ultimo_dia = calendar.monthrange(anio_calc, mes_calc)
    fecha_inicio_mes = date(anio_calc, mes_calc, 1)
    fecha_fin_mes = date(anio_calc, mes_calc, ultimo_dia)

    # Obtenemos los feriados cargados en ese mes para no descontarles horas a los empleados
    feriados_mes_objs = db.query(models.Feriado).filter(
        models.Feriado.fecha >= fecha_inicio_mes,
        models.Feriado.fecha <= fecha_fin_mes
    ).all()
    fechas_feriados = {f.fecha for f in feriados_mes_objs}

    dias_habiles_mes = []
    dia_cursor = fecha_inicio_mes
    while dia_cursor <= fecha_fin_mes:
        if dia_cursor.weekday() < 5 and dia_cursor not in fechas_feriados:
            dias_habiles_mes.append(dia_cursor)
        dia_cursor += timedelta(days=1)

    cantidad_horas_mes = len(dias_habiles_mes) * 6

    query_emp = db.query(models.Empleado).outerjoin(models.Sector).filter(models.Empleado.activo == True)
    if sector_id:
        query_emp = query_emp.filter(models.Empleado.sector_id == sector_id)
    empleados = query_emp.order_by(models.Sector.nombre.asc(), models.Empleado.nombre_completo.asc()).all()

    asistencias_mes = db.query(models.Asistencia).filter(
        models.Asistencia.fecha >= fecha_inicio_mes,
        models.Asistencia.fecha <= fecha_fin_mes
    ).all()

    asis_map = {}
    for a in asistencias_mes:
        asis_map[(a.empleado_id, a.fecha)] = a

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')

    writer.writerow([
        "CUIL",
        "NOMBRE COMPLETO",
        "SECRETARIA",
        "SECTOR",
        "FUNCION",
        "CANTIDAD DE HORAS",
        "HORAS ADICIONALES",
        "HORAS RESTANTES"
    ])

    fecha_tope_evaluacion = min(ahora, fecha_fin_mes)

    for emp in empleados:
        minutos_restantes_acumulados = 0

        for dia_habil in dias_habiles_mes:
            if dia_habil > fecha_tope_evaluacion:
                continue

            reg = asis_map.get((emp.id, dia_habil))
            if not reg:
                minutos_restantes_acumulados += 360
            else:
                estado_real = reg.estado if reg.estado else "Presente"
                if estado_real == "Licencia":
                    minutos_restantes_acumulados += 0
                elif estado_real == "Falta" or not reg.hora_llegada:
                    minutos_restantes_acumulados += 360
                else:
                    calc = calcular_tiempos_jornada(reg.hora_llegada, reg.hora_salida, reg.fecha)
                    faltante_dia = max(0, 360 - calc["minutos_totales"])
                    minutos_restantes_acumulados += faltante_dia

        h_rest = minutos_restantes_acumulados // 60
        m_rest = minutos_restantes_acumulados % 60
        horas_restantes_str = f"{h_rest}h {m_rest:02d}m" if m_rest > 0 else f"{h_rest}"

        sec_nombre = (emp.sector.nombre if emp.sector else "SIN SECTOR").upper()
        secretaria_txt = (getattr(emp, "secretaria", None) or "GOBIERNO").upper()
        funcion_txt = (getattr(emp, "funcion", None) or "ADMINISTRATIVA").upper()

        writer.writerow([
            limpiar_solo_numeros(emp.legajo),
            (emp.nombre_completo or "").upper(),
            secretaria_txt,
            sec_nombre,
            funcion_txt,
            cantidad_horas_mes,
            "",
            horas_restantes_str
        ])

    output.seek(0)
    filename = f"Liquidacion_Hacienda_{mes_calc:02d}_{anio_calc}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/rrhh/exportar-liquidacion")
def exportar_liquidacion_rrhh(
    desde: date,
    hasta: date,
    sector_id: int | None = None,
    dni: str | None = None,
    db: Session = Depends(get_db),
    user: models.Encargado = Depends(obtener_permiso_reportes)
):
    return exportar_excel_diario(desde=desde, hasta=hasta, sector_id=sector_id, dni=dni, db=db, user=user)