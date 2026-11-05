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
        conn.commit()
    except Exception as e:
        print("Aviso migración:", e)

app = FastAPI(title="Tarja Backend API")

# --- HABILITAR CORS PARA PERMITIR CONEXIÓN DESDE EL HOSTING MUNICIPAL ---
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
    """Deja únicamente los números (sin guiones, sin puntos y sin comillas)."""
    if not valor:
        return ""
    return re.sub(r'[^0-9]', '', str(valor))

def registrar_auditoria(db: Session, usuario_obj: models.Encargado, accion: str, detalle: str):
    """Guarda automáticamente quién hizo qué cambio y a qué hora exacta."""
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
    return usuario

# =====================================================================
# LOS 3 NIVELES DE CANDADOS DE SEGURIDAD SEGÚN ROL
# =====================================================================
def obtener_permiso_reportes(usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    """Nivel 1: Permite ver asistencias, PDF y Excel a: admin, carga y reportes."""
    if usuario_actual.rol not in ["admin", "carga", "reportes", "rrhh"]:
        raise HTTPException(
            status_code=403, 
            detail="Acceso denegado: No tiene permisos para visualizar reportes."
        )
    return usuario_actual

def obtener_permiso_carga(usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    """Nivel 2: Permite cargar padrón, secretarías, sectores y funciones a: admin y carga."""
    if usuario_actual.rol not in ["admin", "carga", "rrhh"]:
        raise HTTPException(
            status_code=403, 
            detail="Acceso denegado: Su cuenta es solo de visualización de reportes."
        )
    return usuario_actual

def obtener_admin_total(usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    """Nivel 3: Exclusivo ADMINISTRADOR TOTAL (Ver auditoría y crear usuarios)."""
    if usuario_actual.rol not in ["admin"]:
        raise HTTPException(
            status_code=403, 
            detail="Acceso denegado: Función exclusiva del Administrador Total."
        )
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
    rol: str = "encargado" # "admin", "carga", "reportes" o "encargado"

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
# CATÁLOGOS: SECTORES, SECRETARÍAS Y FUNCIONES
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

# ==========================================
# PADRÓN DE EMPLEADOS E IMPORTACIÓN MASIVA (REQUIERE PERMISO DE CARGA O ADMIN)
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
# GESTIÓN DE USUARIOS Y AUDITORÍA (EXCLUSIVO ADMIN TOTAL)
# ==========================================
@app.get("/admin/encargados")
def ver_encargados_admin(db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_total)):
    encargados = db.query(models.Encargado).order_by(models.Encargado.id).all()
    return [{
        "id": enc.id,
        "usuario": enc.usuario,
        "nombre_completo": getattr(enc, "nombre_completo", None) or enc.usuario,
        "rol": enc.rol,
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
        rol=rol_elegido
    )
    db.add(nuevo_encargado)
    db.commit()

    registrar_auditoria(
        db, admin, "ALTA USUARIO SISTEMA",
        f"Dio de alta al usuario '{nuevo_encargado.usuario}' ({nombre_op}) con rol '{rol_elegido.upper()}'"
    )
    return {"mensaje": f"Usuario {nuevo_encargado.usuario} ({nombre_op}) creado con éxito."}

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
# CONSULTA DE ASISTENCIAS Y REPORTES (PERMITIDO A ADMIN, CARGA Y REPORTES)
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
            raise HTTPException(status_code=400, detail=f"Usted figura hoy con estado: {asistencia.estado}. Consulte con el Administrador.")
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
# EXCEL 1: REPORTE DIARIO DE ASISTENCIAS (PERMITIDO A ADMIN, CARGA Y REPORTES)
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
# EXCEL 2: REPORTE MENSUAL PARA HACIENDA (PERMITIDO A ADMIN, CARGA Y REPORTES)
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

    dias_habiles_mes = []
    dia_cursor = fecha_inicio_mes
    while dia_cursor <= fecha_fin_mes:
        if dia_cursor.weekday() < 5:
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


# =====================================================================
# PORTAL WEB MUNICIPAL (/web) CON CONTROL DE VISIBILIDAD SEGÚN ROL
# =====================================================================
@app.get("/web", response_class=HTMLResponse)
def portal_web_municipal():
    return """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Municipalidad de Tartagal - Tarja Digital</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; }
    </style>
</head>
<body class="bg-slate-100 min-h-screen text-slate-800">
    <nav class="bg-gradient-to-r from-green-800 via-green-700 to-emerald-800 border-b-4 border-green-950 text-white px-6 py-3.5 shadow-lg flex flex-wrap justify-between items-center gap-4">
        <div class="flex items-center space-x-3.5">
            <img src="logo.png" onerror="this.onerror=null; this.src='https://tarja-api.onrender.com/logo.png';" alt="Logo Municipalidad" class="h-12 w-12 rounded-xl bg-white p-1 object-contain shadow-md">
            <div>
                <h1 class="font-extrabold text-xl leading-tight tracking-tight">Municipalidad de Tartagal</h1>
                <p class="text-xs text-emerald-100 font-medium tracking-wide">Sistema de Tarja Digital y Auditoría de Personal</p>
            </div>
        </div>
        <div class="flex items-center space-x-3">
            <button onclick="cambiarPestana('kiosco')" id="btn-tab-kiosco" class="inline-flex items-center gap-2 bg-blue-600 hover:bg-blue-500 text-white ring-2 ring-white/90 px-4 py-2.5 rounded-xl font-semibold text-sm shadow-md transition-all duration-150">
                <span>👤 Terminal Empleados</span>
            </button>
            <button onclick="cambiarPestana('rrhh')" id="btn-tab-rrhh" class="inline-flex items-center gap-2 bg-blue-900/80 hover:bg-blue-800 text-blue-100 border border-blue-400/30 px-4 py-2.5 rounded-xl font-semibold text-sm shadow transition-all duration-150">
                <span>🔐 Iniciar Sesión</span>
            </button>
        </div>
    </nav>

    <!-- VISTA 1: TERMINAL EMPLEADOS -->
    <div id="vista-kiosco" class="max-w-lg mx-auto mt-8 bg-white p-7 rounded-2xl shadow-xl border border-slate-200/80 border-t-4 border-t-green-600 text-center mb-12">
        <h2 class="text-2xl font-extrabold text-slate-800 tracking-tight">Terminal de Autogestión</h2>
        <p class="text-slate-500 text-sm mt-1 mb-4">Ingrese su DNI sin puntos y mire hacia la cámara para registrar su asistencia</p>

        <div class="bg-slate-900 text-emerald-400 font-mono text-4xl font-bold py-3.5 rounded-xl mb-4 shadow-inner tracking-wider border border-slate-800" id="reloj-vivo">
            00:00:00
        </div>

        <div class="relative mx-auto w-64 h-48 bg-slate-900 rounded-xl overflow-hidden border-2 border-green-600 shadow-md mb-3">
            <video id="video-camara" autoplay playsinline muted class="w-full h-full object-cover"></video>
            <canvas id="canvas-foto" width="320" height="240" class="hidden"></canvas>
            <div id="estado-camara" class="absolute bottom-0 inset-x-0 bg-slate-950/80 text-white text-xs py-1.5 font-medium">
                📸 Cámara activa para verificación de identidad
            </div>
        </div>

        <div class="bg-amber-50 border border-amber-200 text-amber-900 text-xs font-medium px-3.5 py-2.5 rounded-xl mb-5">
            ⚠️ Al confirmar Entrada o Salida se capturará una fotografía de auditoría asociada a su DNI.
        </div>

        <input type="number" id="dni-kiosco" placeholder="N° de DNI" autofocus
            class="w-full text-center text-3xl font-bold p-3.5 border-2 border-slate-300 rounded-xl mb-4 focus:border-green-600 focus:outline-none transition">

        <div class="grid grid-cols-2 gap-3.5 mb-3.5">
            <button onclick="registrarFichaje('entrada')" class="bg-gradient-to-b from-emerald-600 to-green-700 hover:from-emerald-500 hover:to-green-600 text-white font-bold py-4 rounded-xl text-base shadow-md">
                🟢 MARCAR ENTRADA
            </button>
            <button onclick="registrarFichaje('salida')" class="bg-gradient-to-b from-amber-500 to-orange-600 hover:from-amber-400 hover:to-orange-500 text-white font-bold py-4 rounded-xl text-base shadow-md">
                🟠 MARCAR SALIDA
            </button>
        </div>

        <button onclick="registrarFichaje('consulta')" class="w-full bg-slate-800 hover:bg-slate-700 text-white font-semibold py-3.5 rounded-xl text-sm shadow">
            ⏱️ CONSULTAR MIS HORAS TRABAJADAS HOY Y EN EL MES
        </button>

        <div id="alerta-kiosco" class="mt-5 p-4 rounded-xl hidden text-left"></div>
    </div>

    <!-- VISTA 2: PANEL ADMINISTRATIVO CON PERMISOS POR ROL -->
    <div id="vista-rrhh" class="max-w-6xl mx-auto mt-8 px-4 hidden pb-16">
        
        <div id="caja-login-rrhh" class="max-w-md mx-auto bg-white p-8 rounded-2xl shadow-xl border border-slate-200 border-t-4 border-t-green-700">
            <div class="text-center mb-6">
                <h2 class="text-2xl font-extrabold text-slate-800 tracking-tight">ACCESO AL SISTEMA</h2>
                <p class="text-slate-500 text-sm mt-1">Inicie sesión con su cuenta de Administrador, Carga de Padrón o Reportes.</p>
            </div>
            <div class="space-y-4">
                <div>
                    <label class="block text-xs font-bold uppercase tracking-wider text-slate-600 mb-1.5">Usuario</label>
                    <input type="text" id="user-rrhh" class="w-full p-3 border border-slate-300 rounded-xl focus:border-green-600 focus:outline-none" placeholder="Ingrese su usuario">
                </div>
                <div>
                    <label class="block text-xs font-bold uppercase tracking-wider text-slate-600 mb-1.5">Contraseña</label>
                    <input type="password" id="pass-rrhh" class="w-full p-3 border border-slate-300 rounded-xl focus:border-green-600 focus:outline-none" placeholder="••••••••">
                </div>
                <div id="error-login" class="text-red-600 text-sm font-semibold hidden"></div>
                <button onclick="iniciarSesionRRHH()" class="w-full bg-green-700 hover:bg-green-800 transition text-white font-bold py-3.5 rounded-xl shadow-md">
                    INGRESAR AL PANEL
                </button>
            </div>
        </div>

        <div id="panel-descargas-rrhh" class="space-y-6 hidden">
            
            <!-- CABECERA CON PESTAÑAS DINÁMICAS SEGÚN EL ROL DEL USUARIO -->
            <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 border-l-8 border-l-green-700 flex flex-wrap justify-between items-center gap-4">
                <div>
                    <span id="badge-rol" class="bg-green-100 text-green-800 text-xs font-extrabold px-3 py-1 rounded-full uppercase tracking-wider">OPERADOR ACTIVO</span>
                    <h2 class="text-2xl font-extrabold text-slate-800 mt-1 tracking-tight">Panel de Gestión Municipal</h2>
                </div>
                <div class="flex flex-wrap items-center gap-2">
                    <button onclick="cambiarSubPestanaAdmin('reportes')" id="btn-sub-reportes" class="bg-blue-600 text-white font-semibold px-3.5 py-2.5 rounded-xl text-xs shadow-sm transition">
                        📊 1. Reportes y Excel
                    </button>
                    <button onclick="cambiarSubPestanaAdmin('personal')" id="btn-sub-personal" class="bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold px-3.5 py-2.5 rounded-xl text-xs border border-slate-300 transition hidden">
                        🪪 2. Padrón y Carga Masiva
                    </button>
                    <button onclick="cambiarSubPestanaAdmin('catalogos')" id="btn-sub-catalogos" class="bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold px-3.5 py-2.5 rounded-xl text-xs border border-slate-300 transition hidden">
                        ⚙️ 3. Secretarías y Sectores
                    </button>
                    <button onclick="cambiarSubPestanaAdmin('usuarios')" id="btn-sub-usuarios" class="bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold px-3.5 py-2.5 rounded-xl text-xs border border-slate-300 transition hidden">
                        👥 4. Usuarios del Sistema
                    </button>
                    <button onclick="cambiarSubPestanaAdmin('auditoria')" id="btn-sub-auditoria" class="bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold px-3.5 py-2.5 rounded-xl text-xs border border-slate-300 transition hidden">
                        🕵️‍♂️ 5. Auditoría Total
                    </button>
                    <button onclick="cerrarSesionRRHH()" class="bg-white hover:bg-red-50 text-red-600 border border-red-200 font-semibold px-3.5 py-2.5 rounded-xl text-xs transition">
                        Salir
                    </button>
                </div>
            </div>

            <!-- SUB-PESTAÑA 1: REPORTES DE ASISTENCIA (VISIBLE PARA ADMIN, CARGA Y REPORTES) -->
            <div id="subvista-reportes" class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200">
                <h3 class="text-lg font-extrabold text-slate-800 mb-4">Reportes de Asistencia, PDF y Planillas Excel</h3>
                
                <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
                    <button onclick="seleccionarPeriodo('dia')" class="bg-slate-50 hover:bg-blue-50 border border-slate-200 text-slate-700 font-semibold py-2.5 px-3 rounded-xl text-xs transition">📅 Día de Hoy</button>
                    <button onclick="seleccionarPeriodo('semana')" class="bg-slate-50 hover:bg-green-50 border border-slate-200 text-slate-700 font-semibold py-2.5 px-3 rounded-xl text-xs transition">📆 Últimos 7 Días</button>
                    <button onclick="seleccionarPeriodo('mes30')" class="bg-slate-50 hover:bg-emerald-50 border border-slate-200 text-slate-700 font-semibold py-2.5 px-3 rounded-xl text-xs transition">📊 Últimos 30 Días</button>
                    <button onclick="seleccionarPeriodo('mes_anterior')" class="bg-slate-50 hover:bg-amber-50 border border-slate-200 text-slate-700 font-semibold py-2.5 px-3 rounded-xl text-xs transition">⏪ Mes Anterior Completo</button>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-4 gap-4 bg-slate-50 p-4 rounded-xl border border-slate-200 mb-5">
                    <div>
                        <label class="block text-xs font-bold text-slate-600 uppercase mb-1">Sector Municipal</label>
                        <select id="filtro-sector" class="w-full p-2.5 border border-slate-300 rounded-lg bg-white font-semibold text-sm">
                            <option value="">Todos los sectores</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-600 uppercase mb-1">Buscar Agente (DNI)</label>
                        <input type="text" id="filtro-dni" placeholder="Ej: 41488846" class="w-full p-2.5 border border-slate-300 rounded-lg bg-white text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-600 uppercase mb-1">Fecha Desde (o Mes Hacienda)</label>
                        <input type="date" id="fecha-desde" class="w-full p-2.5 border border-slate-300 rounded-lg bg-white text-sm">
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-600 uppercase mb-1">Fecha Hasta</label>
                        <input type="date" id="fecha-hasta" class="w-full p-2.5 border border-slate-300 rounded-lg bg-white text-sm">
                    </div>
                </div>

                <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3.5">
                    <button onclick="verTablaEnVivo()" class="bg-slate-800 hover:bg-slate-900 text-white font-semibold py-3.5 px-4 rounded-xl shadow-sm text-sm transition">👁️ Ver en Pantalla</button>
                    <button onclick="descargarPDFAdmin()" class="bg-rose-600 hover:bg-rose-700 text-white font-semibold py-3.5 px-4 rounded-xl shadow-sm text-sm transition">📄 Descargar PDF (Sectores)</button>
                    <button onclick="descargarExcelDiario()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-semibold py-3.5 px-4 rounded-xl shadow-sm text-sm transition">📥 Excel Diario (Asistencias)</button>
                    <button onclick="descargarExcelHacienda()" class="bg-blue-700 hover:bg-blue-800 text-white font-semibold py-3.5 px-4 rounded-xl shadow-sm text-sm transition">🏛️ Excel Mensual Hacienda</button>
                </div>

                <div id="contenedor-tabla-vivo" class="mt-6 hidden">
                    <div class="overflow-x-auto border border-slate-200 rounded-xl">
                        <table class="w-full text-left border-collapse text-sm">
                            <thead class="bg-green-800 text-white text-xs uppercase">
                                <tr>
                                    <th class="p-3.5">Fecha</th>
                                    <th class="p-3.5">DNI / CUIL</th>
                                    <th class="p-3.5">Agente Municipal</th>
                                    <th class="p-3.5">Sector</th>
                                    <th class="p-3.5">Entrada / Salida (6hs)</th>
                                    <th class="p-3.5">Salida Real</th>
                                    <th class="p-3.5">Horas</th>
                                    <th class="p-3.5 text-center">Fotos de Fichaje</th>
                                </tr>
                            </thead>
                            <tbody id="tbody-vivo" class="divide-y divide-slate-200"></tbody>
                        </table>
                    </div>
                </div>
            </div>

            <!-- SUB-PESTAÑA 2: PADRÓN Y CARGA MASIVA (VISIBLE PARA ADMIN Y CARGA) -->
            <div id="subvista-personal" class="space-y-6 hidden">
                <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200">
                    <h3 class="text-xl font-extrabold text-slate-800 mb-1">1. Alta y Edición Individual del Padrón</h3>
                    <p class="text-slate-500 text-sm mb-4">Cargue o modifique agentes asignando su DNI, CUIL sin guiones, Secretaría, Sector y Función.</p>

                    <div class="grid grid-cols-1 md:grid-cols-4 gap-3.5 bg-slate-50 p-4 rounded-xl border border-slate-200 mb-6">
                        <input type="hidden" id="emp-id-edit">
                        <div>
                            <label class="block text-xs font-bold text-slate-600 mb-1">APELLIDO Y NOMBRE</label>
                            <input type="text" id="emp-nombre" placeholder="Ej: CARRIZO FERNANDO" class="w-full p-2.5 border border-slate-300 rounded-lg bg-white text-sm">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-600 mb-1">NÚMERO DE DNI (SOLO NÚMEROS)</label>
                            <input type="number" id="emp-dni" placeholder="Ej: 41488846" class="w-full p-2.5 border border-slate-300 rounded-lg bg-white text-sm">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-600 mb-1">CUIL (SOLO NÚMEROS)</label>
                            <input type="number" id="emp-legajo" placeholder="Ej: 20414888462" class="w-full p-2.5 border border-slate-300 rounded-lg bg-white text-sm">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-600 mb-1">SECTOR</label>
                            <select id="emp-sector" class="w-full p-2.5 border border-slate-300 rounded-lg bg-white text-sm font-medium"></select>
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-600 mb-1">SECRETARÍA</label>
                            <select id="emp-secretaria" class="w-full p-2.5 border border-slate-300 rounded-lg bg-white text-sm font-medium"></select>
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-600 mb-1">FUNCIÓN</label>
                            <select id="emp-funcion" class="w-full p-2.5 border border-slate-300 rounded-lg bg-white text-sm font-medium"></select>
                        </div>
                        <div class="md:col-span-2 flex items-end">
                            <button onclick="guardarEmpleadoWeb()" id="btn-guardar-emp" class="w-full bg-green-700 hover:bg-green-800 text-white font-bold py-2.5 px-4 rounded-lg shadow-sm text-sm transition">
                                ➕ Guardar Agente en Padrón
                            </button>
                        </div>
                    </div>

                    <div class="flex justify-between items-center mb-3">
                        <input type="text" id="buscar-emp-web" oninput="filtrarEmpleadosWeb()" placeholder="🔍 Buscar agente por DNI, Apellido, CUIL o Sector..." class="w-full md:w-1/2 p-2.5 border border-slate-300 rounded-xl text-sm">
                        <span id="contador-empleados" class="text-xs font-bold text-slate-600"></span>
                    </div>

                    <div class="overflow-x-auto border border-slate-200 rounded-xl max-h-96">
                        <table class="w-full text-left border-collapse text-sm">
                            <thead class="bg-slate-100 text-slate-700 sticky top-0 text-xs uppercase">
                                <tr>
                                    <th class="p-3">DNI</th>
                                    <th class="p-3">CUIL</th>
                                    <th class="p-3">Apellido y Nombre</th>
                                    <th class="p-3">Secretaría / Sector</th>
                                    <th class="p-3">Función</th>
                                    <th class="p-3 text-center">Acción</th>
                                </tr>
                            </thead>
                            <tbody id="tbody-empleados" class="divide-y divide-slate-200"></tbody>
                        </table>
                    </div>
                </div>

                <div class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 border-l-8 border-l-blue-600">
                    <h3 class="text-lg font-extrabold text-slate-800 mb-1">📥 2. Importación Masiva del Padrón Municipal (Excel / CSV)</h3>
                    <p class="text-slate-500 text-xs mb-3">
                        Suba un archivo <b>.CSV</b> o pegue las filas desde Excel con las columnas en este orden:<br>
                        <code class="bg-slate-100 text-blue-800 px-2 py-0.5 rounded font-bold">CUIL ; DNI ; NOMBRE COMPLETO ; SECRETARIA ; SECTOR ; FUNCION</code>
                    </p>
                    <div class="mb-3">
                        <input type="file" id="archivo-csv-masivo" accept=".csv,.txt" onchange="leerArchivoCSV(event)" class="text-xs text-slate-600 file:mr-3 file:py-2 file:px-4 file:rounded-lg file:border-0 file:text-xs file:font-bold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100">
                    </div>
                    <textarea id="texto-csv-masivo" rows="4" placeholder="20414888462;41488846;CARRIZO FERNANDO;GOBIERNO;SISTEMA VIRTUAL FORMACION A DISTANCIA;ADMINISTRATIVA" class="w-full p-3 border border-slate-300 rounded-xl font-mono text-xs mb-3"></textarea>
                    <button onclick="ejecutarImportacionMasiva()" class="bg-blue-700 hover:bg-blue-800 text-white font-bold py-2.5 px-6 rounded-xl text-sm shadow">
                        🚀 Procesar e Importar Padrón Completo
                    </button>
                </div>
            </div>

            <!-- SUB-PESTAÑA 3: SECRETARÍAS, SECTORES Y FUNCIONES (VISIBLE PARA ADMIN Y CARGA) -->
            <div id="subvista-catalogos" class="space-y-6 hidden">
                <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
                    <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-200">
                        <h4 class="font-extrabold text-slate-800 mb-2">🏛️ Secretarías</h4>
                        <div class="flex gap-2 mb-3">
                            <input type="text" id="nueva-secretaria" placeholder="Ej: HACIENDA" class="w-full p-2 border rounded-lg text-sm">
                            <button onclick="crearItemCatalogo('secretarias', 'nueva-secretaria')" class="bg-green-700 text-white font-bold px-3.5 rounded-lg text-sm">+</button>
                        </div>
                        <ul id="lista-secretarias" class="text-xs space-y-1.5 max-h-60 overflow-y-auto divide-y"></ul>
                    </div>

                    <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-200">
                        <h4 class="font-extrabold text-slate-800 mb-2">📂 Sectores / Áreas</h4>
                        <div class="flex gap-2 mb-3">
                            <input type="text" id="nuevo-sector" placeholder="Ej: Tránsito" class="w-full p-2 border rounded-lg text-sm">
                            <button onclick="crearItemCatalogo('sectores', 'nuevo-sector')" class="bg-green-700 text-white font-bold px-3.5 rounded-lg text-sm">+</button>
                        </div>
                        <ul id="lista-sectores" class="text-xs space-y-1.5 max-h-60 overflow-y-auto divide-y"></ul>
                    </div>

                    <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-200">
                        <h4 class="font-extrabold text-slate-800 mb-2">🛠️ Funciones</h4>
                        <div class="flex gap-2 mb-3">
                            <input type="text" id="nueva-funcion" placeholder="Ej: OPERATIVA" class="w-full p-2 border rounded-lg text-sm">
                            <button onclick="crearItemCatalogo('funciones', 'nueva-funcion')" class="bg-green-700 text-white font-bold px-3.5 rounded-lg text-sm">+</button>
                        </div>
                        <ul id="lista-funciones" class="text-xs space-y-1.5 max-h-60 overflow-y-auto divide-y"></ul>
                    </div>
                </div>
            </div>

            <!-- SUB-PESTAÑA 4: GESTIÓN DE USUARIOS (EXCLUSIVO ADMIN TOTAL) -->
            <div id="subvista-usuarios" class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 hidden">
                <h3 class="text-lg font-extrabold text-slate-800 mb-1">👥 Gestión de Usuarios y Niveles de Permiso (Exclusivo Admin Total)</h3>
                <p class="text-slate-500 text-xs mb-4">Cree cuentas para el personal asignando su Nombre y Apellido y el nivel exacto de acceso que tendrán en el sistema.</p>

                <div class="grid grid-cols-1 md:grid-cols-5 gap-3 bg-slate-50 p-4 rounded-xl border border-slate-200 mb-5">
                    <input type="text" id="op-nombre" placeholder="Nombre y Apellido (Ej: Ana Gómez)" class="p-2.5 border rounded-lg bg-white text-sm">
                    <input type="text" id="op-usuario" placeholder="Usuario Login (Ej: agomez)" class="p-2.5 border rounded-lg bg-white text-sm">
                    <input type="password" id="op-pass" placeholder="Contraseña" class="p-2.5 border rounded-lg bg-white text-sm">
                    <select id="op-rol" onchange="toggleSectorOperador()" class="p-2.5 border rounded-lg bg-white text-sm font-semibold">
                        <option value="reportes">1. SOLO REPORTES (PDF, Excel y Asistencias)</option>
                        <option value="carga">2. CARGA Y REPORTES (Padrón, Secretarías y Reportes)</option>
                        <option value="admin">3. ADMINISTRADOR TOTAL (Auditoría + Sistema Completo)</option>
                        <option value="encargado">4. ENCARGADO DE SECTOR (App Celular)</option>
                    </select>
                    <select id="op-sector" class="p-2.5 border rounded-lg bg-white text-sm hidden"></select>
                    <button onclick="crearOperadorWeb()" class="md:col-span-5 bg-blue-700 hover:bg-blue-800 text-white font-bold py-2.5 rounded-xl text-sm shadow">
                        ➕ Crear Cuenta de Usuario
                    </button>
                </div>

                <div class="overflow-x-auto border rounded-xl">
                    <table class="w-full text-left text-sm">
                        <thead class="bg-slate-100 text-xs uppercase">
                            <tr>
                                <th class="p-3">Nombre y Apellido</th>
                                <th class="p-3">Usuario (Login)</th>
                                <th class="p-3">Nivel de Permiso</th>
                                <th class="p-3">Alcance / Sector</th>
                            </tr>
                        </thead>
                        <tbody id="tbody-operadores" class="divide-y"></tbody>
                    </table>
                </div>
            </div>

            <!-- SUB-PESTAÑA 5: HISTORIAL DE AUDITORÍA (EXCLUSIVO ADMIN TOTAL) -->
            <div id="subvista-auditoria" class="bg-white p-6 rounded-2xl shadow-sm border border-slate-200 hidden">
                <div class="flex justify-between items-center mb-4">
                    <div>
                        <h3 class="text-xl font-extrabold text-slate-800">🕵️‍♂️ Registro de Auditoría del Sistema (Exclusivo Admin Total)</h3>
                        <p class="text-slate-500 text-xs">Historial de quién realizó cada alta, modificación de DNI/CUIL, creación de secretarías o sincronización.</p>
                    </div>
                    <button onclick="cargarAuditoriaWeb()" class="bg-slate-800 text-white text-xs font-bold px-4 py-2 rounded-lg">🔄 Actualizar</button>
                </div>

                <div class="overflow-x-auto border border-slate-200 rounded-xl max-h-[500px]">
                    <table class="w-full text-left border-collapse text-xs">
                        <thead class="bg-slate-800 text-white uppercase sticky top-0">
                            <tr>
                                <th class="p-3">Fecha y Hora</th>
                                <th class="p-3">Operador (Nombre y Usuario)</th>
                                <th class="p-3">Acción</th>
                                <th class="p-3">Detalle del Cambio</th>
                            </tr>
                        </thead>
                        <tbody id="tbody-auditoria" class="divide-y divide-slate-200"></tbody>
                    </table>
                </div>
            </div>

        </div>
    </div>

    <!-- MODAL PARA VER FOTO EN GRANDE -->
    <div id="modal-foto" onclick="cerrarModalFoto()" class="fixed inset-0 bg-slate-950/80 hidden items-center justify-center z-50 p-4">
        <div class="bg-white p-5 rounded-2xl max-w-md w-full text-center shadow-2xl" onclick="event.stopPropagation()">
            <h4 id="titulo-modal-foto" class="font-bold text-lg text-slate-800 mb-0.5"></h4>
            <p id="subtitulo-modal-foto" class="text-xs text-slate-500 mb-3"></p>
            <img id="img-modal-foto" src="" class="w-full rounded-xl border-2 border-green-600 mb-4">
            <button onclick="cerrarModalFoto()" class="bg-slate-800 hover:bg-slate-900 text-white font-semibold px-6 py-2 rounded-xl text-sm">Cerrar Vista</button>
        </div>
    </div>

    <script>
        const API_BASE = "https://tarja-api.onrender.com";
        let tokenAdmin = localStorage.getItem('tarja_rrhh_token') || null;
        let rolActual = localStorage.getItem('tarja_operador_rol') || 'reportes';
        let operadorActual = localStorage.getItem('tarja_operador_nombre') || 'OPERADOR';
        let listaEmpleadosGlobal = [];
        let camaraLista = false;

        async function iniciarCamara() {
            const video = document.getElementById('video-camara');
            const estado = document.getElementById('estado-camara');
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ video: { width: 320, height: 240, facingMode: "user" }, audio: false });
                video.srcObject = stream;
                camaraLista = true;
                estado.innerText = "📸 Cámara activa para verificación";
            } catch (e) {
                camaraLista = false;
                estado.innerText = "⚠️ Cámara no detectada en este equipo";
            }
        }
        iniciarCamara();

        function capturarFotoBase64() {
            if (!camaraLista) return null;
            const video = document.getElementById('video-camara');
            const canvas = document.getElementById('canvas-foto');
            const ctx = canvas.getContext('2d');
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
            return canvas.toDataURL('image/jpeg', 0.6);
        }

        function obtenerFechaLocalISO(d) {
            const offset = d.getTimezoneOffset() * 60000;
            return new Date(d.getTime() - offset).toISOString().split('T')[0];
        }

        setInterval(() => {
            document.getElementById('reloj-vivo').innerText = new Date().toLocaleTimeString('es-AR', {hour12: false});
        }, 1000);

        document.getElementById('dni-kiosco').addEventListener('keypress', function (e) {
            if (e.key === 'Enter') registrarFichaje('entrada');
        });

        function cambiarPestana(tab) {
            const esKiosco = (tab === 'kiosco');
            document.getElementById('vista-kiosco').classList.toggle('hidden', !esKiosco);
            document.getElementById('vista-rrhh').classList.toggle('hidden', esKiosco);
            if (!esKiosco) verificarEstadoLogin();
        }

        function cambiarSubPestanaAdmin(sub) {
            ['reportes', 'personal', 'catalogos', 'usuarios', 'auditoria'].forEach(nombre => {
                const vista = document.getElementById(`subvista-${nombre}`);
                const btn = document.getElementById(`btn-sub-${nombre}`);
                if (!vista || !btn) return;
                const activo = (nombre === sub);
                vista.classList.toggle('hidden', !activo);
                if (!btn.classList.contains('hidden')) {
                    btn.className = activo
                        ? "bg-blue-600 text-white font-semibold px-3.5 py-2.5 rounded-xl text-xs shadow-sm transition"
                        : "bg-slate-100 hover:bg-slate-200 text-slate-700 font-semibold px-3.5 py-2.5 rounded-xl text-xs border border-slate-300 transition";
                }
            });
            if (sub === 'auditoria') cargarAuditoriaWeb();
            if (sub === 'usuarios') cargarOperadoresWeb();
            if (sub === 'personal') cargarEmpleadosWeb();
        }

        async function registrarFichaje(tipo) {
            const input = document.getElementById('dni-kiosco');
            const dni = input.value.trim();
            const alerta = document.getElementById('alerta-kiosco');

            if (!dni) {
                alerta.className = "mt-5 p-4 rounded-xl bg-amber-100 text-amber-900 border border-amber-300 block font-bold text-center";
                alerta.innerText = "⚠️ Ingrese su número de DNI.";
                return;
            }

            let valorFotoParaBD = null;
            const fotoCapturada = (tipo === 'entrada' || tipo === 'salida') ? capturarFotoBase64() : null;

            if (fotoCapturada) {
                try {
                    const resFoto = await fetch('guardar_foto.php', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({dni: dni, tipo: tipo, foto_base64: fotoCapturada})
                    });
                    if (resFoto.ok) {
                        const dataFoto = await resFoto.json();
                        valorFotoParaBD = dataFoto.url_foto;
                    } else {
                        valorFotoParaBD = fotoCapturada;
                    }
                } catch (_) {
                    valorFotoParaBD = fotoCapturada;
                }
            }

            try {
                const res = await fetch(`${API_BASE}/kiosco/fichar`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({dni: dni, tipo: tipo, foto_base64: valorFotoParaBD})
                });
                const data = await res.json();

                if (res.ok) {
                    alerta.className = "mt-5 p-5 rounded-xl bg-emerald-50 text-emerald-950 border-2 border-emerald-500 block";
                    alerta.innerHTML = `
                        <div class="text-xl font-extrabold">${data.empleado}</div>
                        <div class="text-xs uppercase text-emerald-700 font-bold mb-2">Sector: ${data.sector}</div>
                        <div class="font-bold text-base">${data.mensaje}</div>
                        <div class="mt-2 text-xs bg-white p-2.5 rounded-lg border border-emerald-200 font-semibold text-slate-700">${data.submensaje}</div>
                    `;
                    if (tipo !== 'consulta') input.value = "";
                } else {
                    alerta.className = "mt-5 p-5 rounded-xl bg-red-50 text-red-900 border-2 border-red-300 block font-bold";
                    alerta.innerText = "❌ " + (data.detail || "No se pudo procesar.");
                }
            } catch (err) {
                alerta.className = "mt-5 p-5 rounded-xl bg-red-50 text-red-900 block font-bold";
                alerta.innerText = "❌ Error de conexión con el servidor.";
            }
        }

        async function iniciarSesionRRHH() {
            const u = document.getElementById('user-rrhh').value.trim();
            const p = document.getElementById('pass-rrhh').value.trim();
            const errBox = document.getElementById('error-login');
            errBox.classList.add('hidden');

            const formData = new URLSearchParams();
            formData.append('username', u);
            formData.append('password', p);

            try {
                const res = await fetch(`${API_BASE}/login`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: formData
                });
                const data = await res.json();

                if (res.ok) {
                    if (!['admin', 'carga', 'reportes', 'rrhh'].includes(data.rol)) {
                        errBox.innerText = "Acceso denegado: Cuenta exclusiva de cuadrilla móvil.";
                        errBox.classList.remove('hidden');
                        return;
                    }
                    tokenAdmin = data.access_token;
                    rolActual = data.rol;
                    operadorActual = data.nombre_completo || data.usuario;
                    localStorage.setItem('tarja_rrhh_token', tokenAdmin);
                    localStorage.setItem('tarja_operador_rol', rolActual);
                    localStorage.setItem('tarja_operador_nombre', operadorActual);
                    verificarEstadoLogin();
                } else {
                    errBox.innerText = data.detail || "Credenciales inválidas.";
                    errBox.classList.remove('hidden');
                }
            } catch (e) {
                errBox.innerText = "Error de red.";
                errBox.classList.remove('hidden');
            }
        }

        function cerrarSesionRRHH() {
            tokenAdmin = null;
            localStorage.removeItem('tarja_rrhh_token');
            localStorage.removeItem('tarja_operador_rol');
            localStorage.removeItem('tarja_operador_nombre');
            verificarEstadoLogin();
        }

        function configurarBotonesSegunRol() {
            const esAdminTotal = (rolActual === 'admin');
            const puedeCargar = (rolActual === 'admin' || rolActual === 'carga' || rolActual === 'rrhh');

            document.getElementById('btn-sub-personal').classList.toggle('hidden', !puedeCargar);
            document.getElementById('btn-sub-catalogos').classList.toggle('hidden', !puedeCargar);
            document.getElementById('btn-sub-usuarios').classList.toggle('hidden', !esAdminTotal);
            document.getElementById('btn-sub-auditoria').classList.toggle('hidden', !esAdminTotal);

            let etiquetaRol = "SOLO REPORTES";
            if (esAdminTotal) etiquetaRol = "ADMINISTRADOR TOTAL";
            else if (puedeCargar) etiquetaRol = "CARGA DE PADRÓN Y REPORTES";

            document.getElementById('badge-rol').innerText = `${operadorActual} (${etiquetaRol})`;
            cambiarSubPestanaAdmin('reportes');
        }

        async function cargarCatalogosCompletos() {
            const headers = {'Authorization': 'Bearer ' + tokenAdmin};
            const [resSec, resSecretarias, resFunciones] = await Promise.all([
                fetch(`${API_BASE}/admin/sectores`, {headers}),
                fetch(`${API_BASE}/admin/secretarias`, {headers}),
                fetch(`${API_BASE}/admin/funciones`, {headers})
            ]);

            if (resSec.ok) {
                const sectores = await resSec.json();
                const selFiltro = document.getElementById('filtro-sector');
                const selEmp = document.getElementById('emp-sector');
                const selOp = document.getElementById('op-sector');
                const ulSec = document.getElementById('lista-sectores');

                selFiltro.innerHTML = '<option value="">Todos los sectores</option>';
                selEmp.innerHTML = '';
                selOp.innerHTML = '';
                ulSec.innerHTML = '';

                sectores.forEach(s => {
                    selFiltro.innerHTML += `<option value="${s.id}">${s.nombre}</option>`;
                    selEmp.innerHTML += `<option value="${s.id}">${s.nombre}</option>`;
                    selOp.innerHTML += `<option value="${s.id}">${s.nombre}</option>`;
                    ulSec.innerHTML += `<li class="py-1.5 font-medium text-slate-700">• ${s.nombre}</li>`;
                });
            }

            if (resSecretarias.ok) {
                const secretarias = await resSecretarias.json();
                const selSec = document.getElementById('emp-secretaria');
                const ulSecretarias = document.getElementById('lista-secretarias');
                selSec.innerHTML = '';
                ulSecretarias.innerHTML = '';
                secretarias.forEach(sec => {
                    selSec.innerHTML += `<option value="${sec.nombre}">${sec.nombre}</option>`;
                    ulSecretarias.innerHTML += `<li class="py-1.5 font-medium text-slate-700">• ${sec.nombre}</li>`;
                });
            }

            if (resFunciones.ok) {
                const funciones = await resFunciones.json();
                const selFn = document.getElementById('emp-funcion');
                const ulFn = document.getElementById('lista-funciones');
                selFn.innerHTML = '';
                ulFn.innerHTML = '';
                funciones.forEach(f => {
                    selFn.innerHTML += `<option value="${f.nombre}">${f.nombre}</option>`;
                    ulFn.innerHTML += `<li class="py-1.5 font-medium text-slate-700">• ${f.nombre}</li>`;
                });
            }
        }

        async function verificarEstadoLogin() {
            const loginBox = document.getElementById('caja-login-rrhh');
            const panelBox = document.getElementById('panel-descargas-rrhh');

            if (!tokenAdmin) {
                loginBox.classList.remove('hidden');
                panelBox.classList.add('hidden');
                return;
            }

            const res = await fetch(`${API_BASE}/admin/sectores`, {
                headers: {'Authorization': 'Bearer ' + tokenAdmin}
            });

            if (res.ok) {
                configurarBotonesSegunRol();
                await cargarCatalogosCompletos();
                seleccionarPeriodo('dia');
                loginBox.classList.add('hidden');
                panelBox.classList.remove('hidden');
            } else {
                cerrarSesionRRHH();
            }
        }

        async function crearItemCatalogo(tipo, inputId) {
            const val = document.getElementById(inputId).value.trim();
            if (!val) return;
            const res = await fetch(`${API_BASE}/admin/${tipo}`, {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + tokenAdmin,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({nombre: val})
            });
            const data = await res.json();
            if (res.ok) {
                document.getElementById(inputId).value = '';
                await cargarCatalogosCompletos();
                alert(data.mensaje);
            } else {
                alert(data.detail || "No se pudo agregar.");
            }
        }

        function toggleSectorOperador() {
            const rol = document.getElementById('op-rol').value;
            document.getElementById('op-sector').classList.toggle('hidden', rol !== 'encargado');
        }

        async function cargarOperadoresWeb() {
            const res = await fetch(`${API_BASE}/admin/encargados`, {
                headers: {'Authorization': 'Bearer ' + tokenAdmin}
            });
            if (res.ok) {
                const lista = await res.json();
                const tbody = document.getElementById('tbody-operadores');
                tbody.innerHTML = '';
                lista.forEach(op => {
                    let etiqueta = "ENCARGADO DE SECTOR";
                    let color = "bg-slate-100 text-slate-800";
                    if (op.rol === 'admin') { etiqueta = "ADMINISTRADOR TOTAL"; color = "bg-purple-100 text-purple-800"; }
                    else if (op.rol === 'carga' || op.rol === 'rrhh') { etiqueta = "REPORTES + CARGA PADRÓN"; color = "bg-emerald-100 text-emerald-800"; }
                    else if (op.rol === 'reportes') { etiqueta = "SOLO REPORTES (PDF/EXCEL)"; color = "bg-blue-100 text-blue-800"; }

                    tbody.innerHTML += `
                        <tr class="hover:bg-slate-50">
                            <td class="p-3 font-bold">${op.nombre_completo}</td>
                            <td class="p-3 font-mono text-xs">${op.usuario}</td>
                            <td class="p-3"><span class="px-2.5 py-1 rounded-full text-xs font-bold ${color}">${etiqueta}</span></td>
                            <td class="p-3 text-xs">${op.sector_nombre}</td>
                        </tr>
                    `;
                });
            }
        }

        async function crearOperadorWeb() {
            const nombre_completo = document.getElementById('op-nombre').value.trim();
            const usuario = document.getElementById('op-usuario').value.trim();
            const password = document.getElementById('op-pass').value.trim();
            const rol = document.getElementById('op-rol').value;
            const sector_id = parseInt(document.getElementById('op-sector').value);

            if (!nombre_completo || !usuario || !password) {
                alert("Complete Nombre y Apellido, Usuario y Contraseña.");
                return;
            }

            const res = await fetch(`${API_BASE}/admin/encargados`, {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + tokenAdmin,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    nombre_completo: nombre_completo,
                    usuario: usuario,
                    password: password,
                    rol: rol,
                    sector_id: rol === 'encargado' ? sector_id : null
                })
            });
            const data = await res.json();
            if (res.ok) {
                alert(data.mensaje);
                document.getElementById('op-nombre').value = '';
                document.getElementById('op-usuario').value = '';
                document.getElementById('op-pass').value = '';
                cargarOperadoresWeb();
            } else {
                alert(data.detail || "No se pudo crear el usuario.");
            }
        }

        async function cargarAuditoriaWeb() {
            const res = await fetch(`${API_BASE}/admin/auditoria`, {
                headers: {'Authorization': 'Bearer ' + tokenAdmin}
            });
            if (res.ok) {
                const logs = await res.json();
                const tbody = document.getElementById('tbody-auditoria');
                tbody.innerHTML = logs.length === 0 ? '<tr><td colspan="4" class="p-4 text-center text-slate-400">Sin movimientos registrados aún.</td></tr>' : '';
                logs.forEach(l => {
                    tbody.innerHTML += `
                        <tr class="hover:bg-slate-50">
                            <td class="p-3 font-mono whitespace-nowrap">${l.fecha_hora}</td>
                            <td class="p-3"><b>${l.nombre_operador}</b> <span class="text-slate-400">(${l.usuario})</span></td>
                            <td class="p-3"><span class="bg-emerald-100 text-emerald-900 font-bold px-2 py-0.5 rounded">${l.accion}</span></td>
                            <td class="p-3 text-slate-700">${l.detalle}</td>
                        </tr>
                    `;
                });
            }
        }

        function leerArchivoCSV(event) {
            const archivo = event.target.files[0];
            if (!archivo) return;
            const lector = new FileReader();
            lector.onload = function(e) {
                document.getElementById('texto-csv-masivo').value = e.target.result;
            };
            lector.readAsText(archivo, 'UTF-8');
        }

        async function ejecutarImportacionMasiva() {
            const texto = document.getElementById('texto-csv-masivo').value.trim();
            if (!texto) {
                alert("Pegue las filas desde Excel o seleccione un archivo CSV.");
                return;
            }
            const res = await fetch(`${API_BASE}/admin/importar-masivo`, {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + tokenAdmin,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({csv_texto: texto})
            });
            const data = await res.json();
            if (res.ok) {
                alert(data.mensaje);
                document.getElementById('texto-csv-masivo').value = '';
                await cargarCatalogosCompletos();
                cargarEmpleadosWeb();
            } else {
                alert("Error en importación: " + (data.detail || "Verifique el formato"));
            }
        }

        function seleccionarPeriodo(tipo) {
            const hoy = new Date();
            let desde = new Date();
            let hasta = new Date();

            if (tipo === 'semana') {
                desde.setDate(hoy.getDate() - 7);
            } else if (tipo === 'mes30') {
                desde.setDate(hoy.getDate() - 30);
            } else if (tipo === 'mes_anterior') {
                desde = new Date(hoy.getFullYear(), hoy.getMonth() - 1, 1);
                hasta = new Date(hoy.getFullYear(), hoy.getMonth(), 0);
            }

            document.getElementById('fecha-desde').value = obtenerFechaLocalISO(desde);
            document.getElementById('fecha-hasta').value = obtenerFechaLocalISO(hasta);
            
            if (!document.getElementById('contenedor-tabla-vivo').classList.contains('hidden')) {
                verTablaEnVivo();
            }
        }

        function verFotoGrande(src, nombre, dni, fecha, tipoHora) {
            document.getElementById('titulo-modal-foto').innerText = nombre + " (DNI: " + dni + ")";
            document.getElementById('subtitulo-modal-foto').innerText = "Fecha: " + fecha + " | " + tipoHora;
            document.getElementById('img-modal-foto').src = src;
            const modal = document.getElementById('modal-foto');
            modal.classList.remove('hidden');
            modal.classList.add('flex');
        }

        function cerrarModalFoto() {
            const modal = document.getElementById('modal-foto');
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }

        async function verTablaEnVivo() {
            const desde = document.getElementById('fecha-desde').value || obtenerFechaLocalISO(new Date());
            let hasta = document.getElementById('fecha-hasta').value;
            if (!hasta) {
                hasta = obtenerFechaLocalISO(new Date());
                document.getElementById('fecha-hasta').value = hasta;
            }
            const secFiltro = document.getElementById('filtro-sector').value;
            const dniFiltro = document.getElementById('filtro-dni').value.trim();

            let url = `${API_BASE}/admin/asistencias/${desde}?hasta=${hasta}`;
            if (secFiltro) url += `&sector_id=${secFiltro}`;
            if (dniFiltro) url += `&dni=${encodeURIComponent(dniFiltro)}`;

            const res = await fetch(url, { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
            if (res.ok) {
                const datos = await res.json();
                const tbody = document.getElementById('tbody-vivo');
                tbody.innerHTML = datos.length === 0 
                    ? '<tr><td colspan="8" class="p-6 text-center text-slate-500">No hay asistencias registradas en ese rango de fechas.</td></tr>' 
                    : '';
                
                datos.sort((a, b) => (a.sector || '').localeCompare(b.sector || ''));
                let sectorActual = null;

                datos.forEach(a => {
                    if (a.sector !== sectorActual) {
                        sectorActual = a.sector;
                        tbody.innerHTML += `
                            <tr class="bg-emerald-50/90 border-y border-emerald-200">
                                <td colspan="8" class="p-2.5 font-extrabold text-green-900 uppercase tracking-wider text-xs">
                                    📂 SECTOR: ${sectorActual}
                                </td>
                            </tr>
                        `;
                    }

                    const btnFotoIn = a.foto_llegada 
                        ? `<button onclick="verFotoGrande('${a.foto_llegada}', '${a.nombre}', '${a.dni}', '${a.fecha}', 'Entrada: ${a.hora_llegada}')" class="bg-emerald-100 text-emerald-800 px-2.5 py-1 rounded-lg text-xs font-bold hover:bg-emerald-200 mr-1">📸 Entrada</button>`
                        : `<span class="text-xs text-slate-400 mr-1">Sin foto</span>`;
                    const btnFotoOut = a.foto_salida 
                        ? `<button onclick="verFotoGrande('${a.foto_salida}', '${a.nombre}', '${a.dni}', '${a.fecha}', 'Salida: ${a.hora_salida}')" class="bg-orange-100 text-orange-800 px-2.5 py-1 rounded-lg text-xs font-bold hover:bg-orange-200">📸 Salida</button>`
                        : ``;

                    const cuilLimpio = (a.legajo || '').toString().replace(/[^0-9]/g, '');

                    tbody.innerHTML += `
                        <tr class="hover:bg-slate-50">
                            <td class="p-3.5 font-mono text-xs">${a.fecha}</td>
                            <td class="p-3.5 font-mono"><b>${a.dni || 'Sin DNI'}</b><br><span class="text-xs text-slate-500">CUIL: ${cuilLimpio || '-'}</span></td>
                            <td class="p-3.5 font-bold text-slate-800">${a.nombre}</td>
                            <td class="p-3.5 text-xs font-semibold text-green-800">${a.sector}</td>
                            <td class="p-3.5"><span class="text-emerald-700 font-bold">${a.hora_llegada || '-'}</span><br><span class="text-xs text-slate-500">Sale 6hs: ${a.salida_estimada || '-'}</span></td>
                            <td class="p-3.5 text-orange-600 font-semibold">${a.hora_salida || (a.en_curso ? 'Trabajando...' : '-')}</td>
                            <td class="p-3.5 font-bold ${a.en_curso ? 'text-blue-700' : 'text-emerald-800'}">${a.estado !== 'Presente' ? a.estado : a.tiempo_trabajado}</td>
                            <td class="p-3.5 text-center">${btnFotoIn}${btnFotoOut}</td>
                        </tr>
                    `;
                });
                document.getElementById('contenedor-tabla-vivo').classList.remove('hidden');
            }
        }

        async function descargarPDFAdmin() {
            const desde = document.getElementById('fecha-desde').value || obtenerFechaLocalISO(new Date());
            let hasta = document.getElementById('fecha-hasta').value;
            if (!hasta) {
                hasta = obtenerFechaLocalISO(new Date());
                document.getElementById('fecha-hasta').value = hasta;
            }
            const sec = document.getElementById('filtro-sector').value;
            const dni = document.getElementById('filtro-dni').value.trim();

            let url = `${API_BASE}/admin/reporte_pdf?fecha=${desde}&hasta=${hasta}`;
            if (sec) url += `&sector_id=${sec}`;
            if (dni) url += `&dni=${encodeURIComponent(dni)}`;

            const res = await fetch(url, { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
            if (res.ok) {
                const blob = await res.blob();
                const link = document.createElement('a');
                link.href = window.URL.createObjectURL(blob);
                link.download = `Reporte_Tarja_Por_Sectores_${desde}_al_${hasta}.pdf`;
                link.click();
            } else {
                alert("No hay asistencias registradas en ese rango de fechas para generar el PDF.");
            }
        }

        async function descargarExcelDiario() {
            const d = document.getElementById('fecha-desde').value || obtenerFechaLocalISO(new Date());
            let h = document.getElementById('fecha-hasta').value;
            if (!h) {
                h = obtenerFechaLocalISO(new Date());
                document.getElementById('fecha-hasta').value = h;
            }
            const sec = document.getElementById('filtro-sector').value;
            const dni = document.getElementById('filtro-dni').value.trim();

            let url = `${API_BASE}/rrhh/exportar-diario?desde=${d}&hasta=${h}`;
            if (sec) url += `&sector_id=${sec}`;
            if (dni) url += `&dni=${encodeURIComponent(dni)}`;

            const res = await fetch(url, { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
            if (res.ok) {
                const blob = await res.blob();
                const link = document.createElement('a');
                link.href = window.URL.createObjectURL(blob);
                link.download = `Asistencias_Diarias_${d}_al_${h}.csv`;
                link.click();
            } else {
                alert("Error al descargar el Excel Diario.");
            }
        }

        async function descargarExcelHacienda() {
            const d = document.getElementById('fecha-desde').value || obtenerFechaLocalISO(new Date());
            const sec = document.getElementById('filtro-sector').value;

            let url = `${API_BASE}/rrhh/exportar-mensual-hacienda?desde=${d}`;
            if (sec) url += `&sector_id=${sec}`;

            const res = await fetch(url, { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
            if (res.ok) {
                const blob = await res.blob();
                const link = document.createElement('a');
                link.href = window.URL.createObjectURL(blob);
                link.download = `Liquidacion_Mensual_Hacienda_${d.substring(0, 7)}.csv`;
                link.click();
            } else {
                alert("Error al descargar el Excel Mensual de Hacienda.");
            }
        }

        async function cargarEmpleadosWeb() {
            const res = await fetch(`${API_BASE}/admin/empleados`, { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
            if (res.ok) {
                listaEmpleadosGlobal = await res.json();
                document.getElementById('contador-empleados').innerText = `Total registrados: ${listaEmpleadosGlobal.length} agentes`;
                renderizarEmpleadosWeb(listaEmpleadosGlobal);
            }
        }

        function renderizarEmpleadosWeb(lista) {
            const tbody = document.getElementById('tbody-empleados');
            tbody.innerHTML = '';
            lista.forEach(emp => {
                const cuilLimpio = (emp.legajo || '').toString().replace(/[^0-9]/g, '');
                tbody.innerHTML += `
                    <tr class="hover:bg-slate-50">
                        <td class="p-3 font-mono font-bold ${emp.dni ? 'text-green-800' : 'text-red-500'}">${emp.dni || 'Falta DNI'}</td>
                        <td class="p-3 font-mono">${cuilLimpio}</td>
                        <td class="p-3 font-semibold text-slate-800">${emp.nombre_completo}</td>
                        <td class="p-3 text-xs"><b>${emp.secretaria || 'GOBIERNO'}</b><br><span class="text-slate-500">${emp.sector_nombre}</span></td>
                        <td class="p-3 text-xs font-medium">${emp.funcion || 'ADMINISTRATIVA'}</td>
                        <td class="p-3 text-center">
                            <button onclick='cargarParaEditar(${JSON.stringify(emp)})' class="bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 px-3 py-1 rounded-lg font-bold text-xs">
                                ✏️ Editar
                            </button>
                        </td>
                    </tr>
                `;
            });
        }

        function filtrarEmpleadosWeb() {
            const q = document.getElementById('buscar-emp-web').value.toLowerCase();
            const filtrados = listaEmpleadosGlobal.filter(e => 
                (e.dni || '').toLowerCase().includes(q) || 
                e.nombre_completo.toLowerCase().includes(q) || 
                (e.legajo || '').toLowerCase().includes(q) ||
                (e.sector_nombre || '').toLowerCase().includes(q)
            );
            renderizarEmpleadosWeb(filtrados);
        }

        function cargarParaEditar(emp) {
            document.getElementById('emp-id-edit').value = emp.id;
            document.getElementById('emp-nombre').value = emp.nombre_completo;
            document.getElementById('emp-dni').value = emp.dni || '';
            document.getElementById('emp-legajo').value = (emp.legajo || '').toString().replace(/[^0-9]/g, '');
            document.getElementById('emp-sector').value = emp.sector_id;
            document.getElementById('emp-secretaria').value = emp.secretaria || 'GOBIERNO';
            document.getElementById('emp-funcion').value = emp.funcion || 'ADMINISTRATIVA';
            document.getElementById('btn-guardar-emp').innerText = "💾 Actualizar Agente";
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }

        async function guardarEmpleadoWeb() {
            const idEdit = document.getElementById('emp-id-edit').value;
            const nombre = document.getElementById('emp-nombre').value.trim();
            const dni = document.getElementById('emp-dni').value.trim().replace(/[^0-9]/g, '');
            const legajo = document.getElementById('emp-legajo').value.trim().replace(/[^0-9]/g, '');
            const sector_id = parseInt(document.getElementById('emp-sector').value);
            const secretaria = document.getElementById('emp-secretaria').value || 'GOBIERNO';
            const funcion = document.getElementById('emp-funcion').value || 'ADMINISTRATIVA';

            if (!nombre || !dni || !legajo || !sector_id) {
                alert("Complete Apellido y Nombre, DNI, CUIL y Sector.");
                return;
            }

            const url = idEdit ? `${API_BASE}/admin/empleados/${idEdit}` : `${API_BASE}/admin/empleados`;
            const metodo = idEdit ? 'PUT' : 'POST';

            const res = await fetch(url, {
                method: metodo,
                headers: {
                    'Authorization': 'Bearer ' + tokenAdmin,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    nombre_completo: nombre,
                    dni: dni,
                    legajo: legajo,
                    sector_id: sector_id,
                    secretaria: secretaria,
                    funcion: funcion
                })
            });
            const data = await res.json();

            if (res.ok) {
                alert(data.mensaje);
                document.getElementById('emp-id-edit').value = '';
                document.getElementById('emp-nombre').value = '';
                document.getElementById('emp-dni').value = '';
                document.getElementById('emp-legajo').value = '';
                document.getElementById('btn-guardar-emp').innerText = "➕ Guardar Agente en Padrón";
                cargarEmpleadosWeb();
            } else {
                alert("Error: " + (data.detail || "No se pudo guardar"));
            }
        }
    </script>
</body>
</html>
    """