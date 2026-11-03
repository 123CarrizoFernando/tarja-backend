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

# Migración automática segura para agregar las columnas nuevas en Neon PostgreSQL sin tocar nada a mano
with engine.connect() as conn:
    try:
        conn.execute(text("ALTER TABLE asistencias ADD COLUMN IF NOT EXISTS foto_llegada TEXT"))
        conn.execute(text("ALTER TABLE asistencias ADD COLUMN IF NOT EXISTS foto_salida TEXT"))
        conn.execute(text("ALTER TABLE empleados ADD COLUMN IF NOT EXISTS secretaria TEXT DEFAULT 'GOBIERNO'"))
        conn.execute(text("ALTER TABLE empleados ADD COLUMN IF NOT EXISTS funcion TEXT DEFAULT 'ADMINISTRATIVA'"))
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

def obtener_admin_actual(usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    if usuario_actual.rol not in ["admin", "rrhh"]:
        raise HTTPException(
            status_code=403, 
            detail="Acceso denegado: Se requieren permisos de Administrador."
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
class SectorNuevo(BaseModel):
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
    password: str
    sector_id: int | None = None
    rol: str = "encargado"

class FichajeKioscoRequest(BaseModel):
    dni: str
    tipo: str # "entrada", "salida" o "consulta"
    foto_base64: str | None = None

class CorreoRequest(BaseModel):
    correo_destino: str

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
        "usuario": usuario.usuario
    }

# ==========================================
# RUTAS DE ADMINISTRADOR
# ==========================================
@app.get("/admin/sectores")
def ver_sectores_admin(db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    return db.query(models.Sector).all()

@app.post("/admin/sectores")
def crear_sector_admin(sector: SectorNuevo, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    existe = db.query(models.Sector).filter(models.Sector.nombre == sector.nombre).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ese sector ya existe.")
    nuevo_sector = models.Sector(nombre=sector.nombre)
    db.add(nuevo_sector)
    db.commit()
    db.refresh(nuevo_sector)
    return {"mensaje": "Sector creado con éxito", "sector": nuevo_sector}

@app.get("/admin/empleados")
def ver_todos_los_empleados_admin(sector_id: int | None = None, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
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
def crear_empleado_admin(empleado: EmpleadoNuevo, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    dni_limpio = limpiar_solo_numeros(empleado.dni)
    cuil_limpio = limpiar_solo_numeros(empleado.legajo)

    existe = db.query(models.Empleado).filter(
        (models.Empleado.dni == dni_limpio) | (models.Empleado.legajo == cuil_limpio)
    ).first()
    if existe:
        raise HTTPException(status_code=400, detail="El DNI o CUIL/Legajo ya está registrado en la base de datos.")

    nuevo_empleado = models.Empleado(
        dni=dni_limpio,
        nombre_completo=empleado.nombre_completo.strip(),
        legajo=cuil_limpio,
        secretaria=empleado.secretaria.strip().upper(),
        funcion=empleado.funcion.strip().upper(),
        sector_id=empleado.sector_id,
        activo=True
    )
    db.add(nuevo_empleado)
    db.commit()
    return {"mensaje": f"Empleado {empleado.nombre_completo} (DNI: {dni_limpio}) registrado con éxito"}

@app.put("/admin/empleados/{empleado_id}")
def editar_empleado_admin(empleado_id: int, datos: EmpleadoNuevo, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
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
    
    empleado.nombre_completo = datos.nombre_completo.strip()
    empleado.dni = dni_limpio
    empleado.legajo = cuil_limpio
    empleado.secretaria = datos.secretaria.strip().upper()
    empleado.funcion = datos.funcion.strip().upper()
    empleado.sector_id = datos.sector_id
    db.commit()
    return {"mensaje": "Datos actualizados correctamente."}

# --- VER ASISTENCIAS CON FILTRO DE FECHAS, SECTOR, DNI Y FOTOS ---
@app.get("/admin/asistencias/{fecha_solicitada}")
def obtener_todas_asistencias(
    fecha_solicitada: date,
    hasta: date | None = None,
    sector_id: int | None = None,
    dni: str | None = None,
    db: Session = Depends(get_db),
    admin: models.Encargado = Depends(obtener_admin_actual)
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

@app.get("/admin/encargados")
def ver_encargados_admin(db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    encargados = db.query(models.Encargado).all()
    return [{
        "id": enc.id,
        "usuario": enc.usuario,
        "rol": enc.rol,
        "sector_nombre": enc.sector.nombre if enc.sector else "Global (Todos los sectores)"
    } for enc in encargados]

@app.post("/admin/encargados")
def crear_encargado_admin(datos: EncargadoNuevoAdmin, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    existe = db.query(models.Encargado).filter(models.Encargado.usuario == datos.usuario.strip()).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ese nombre de usuario ya existe.")
    
    rol_elegido = datos.rol if datos.rol in ["admin", "encargado"] else "encargado"
    nuevo_encargado = models.Encargado(
        usuario=datos.usuario.strip(),
        password_hash=get_password_hash(datos.password),
        sector_id=datos.sector_id if rol_elegido == "encargado" else None,
        rol=rol_elegido
    )
    db.add(nuevo_encargado)
    db.commit()
    return {"mensaje": f"Usuario {datos.usuario} creado con éxito."}

@app.get("/admin/dashboard")
def obtener_dashboard_admin(db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
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

# --- RUTA PARA SERVIR EL LOGO MUNICIPAL A LA WEB ---
@app.get("/logo.png")
def obtener_logo_muni():
    if os.path.exists("logo_muni_web.png"):
        return FileResponse("logo_muni_web.png")
    if os.path.exists("logo_muni.png"):
        return FileResponse("logo_muni.png")
    raise HTTPException(status_code=404, detail="Logo no encontrado")

# ==========================================
# DESCARGA DE PDF SEPARADO POR SECTORES (WEB Y APP)
# ==========================================
@app.get("/admin/reporte_pdf")
def descargar_pdf_admin(
    fecha: date | None = None,
    hasta: date | None = None,
    sector_id: int | None = None,
    dni: str | None = None,
    db: Session = Depends(get_db),
    admin: models.Encargado = Depends(obtener_admin_actual)
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
    if usuario_actual.rol in ["admin", "rrhh"] and not usuario_actual.sector_id:
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
# EXCEL 1: REPORTE DIARIO DE ASISTENCIAS (LIMPIO Y CON HORAS RESTADAS)
# =====================================================================
@app.get("/rrhh/exportar-diario")
def exportar_excel_diario(
    desde: date,
    hasta: date,
    sector_id: int | None = None,
    dni: str | None = None,
    db: Session = Depends(get_db),
    admin: models.Encargado = Depends(obtener_admin_actual)
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

    # Encabezado único pedido (sin Salida Exigida, sin títulos ni líneas extras)
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

        # Si debía cumplir 6 hs (360 min), calculamos cuánto le faltó trabajar en el día
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
# EXCEL 2: REPORTE MENSUAL PARA HACIENDA (TODOS LOS SECTORES JUNTOS)
# =====================================================================
@app.get("/rrhh/exportar-mensual-hacienda")
def exportar_mensual_hacienda(
    mes: int | None = None,
    anio: int | None = None,
    desde: date | None = None,
    hasta: date | None = None,
    sector_id: int | None = None,
    db: Session = Depends(get_db),
    admin: models.Encargado = Depends(obtener_admin_actual)
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

    # Contamos todos los días hábiles de Lunes a Viernes del mes
    dias_habiles_mes = []
    dia_cursor = fecha_inicio_mes
    while dia_cursor <= fecha_fin_mes:
        if dia_cursor.weekday() < 5: # 0=Lunes ... 4=Viernes
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

# Mantenemos compatibilidad con el link anterior redirigiendo al Excel Diario limpio
@app.get("/rrhh/exportar-liquidacion")
def exportar_liquidacion_rrhh(
    desde: date,
    hasta: date,
    sector_id: int | None = None,
    dni: str | None = None,
    db: Session = Depends(get_db),
    admin: models.Encargado = Depends(obtener_admin_actual)
):
    return exportar_excel_diario(desde=desde, hasta=hasta, sector_id=sector_id, dni=dni, db=db, admin=admin)


# =====================================================================
# PORTAL WEB MUNICIPAL (/web) CON COLORES DE TARTAGAL Y 2 BOTONES EXCEL
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
    </head>
    <body class="bg-emerald-50/40 min-h-screen font-sans">
        <nav class="bg-green-700 border-b-4 border-green-900 text-white px-6 py-3.5 shadow-lg flex flex-wrap justify-between items-center gap-4">
            <div class="flex items-center space-x-3">
                <img src="/logo.png" onerror="this.style.display='none'" alt="Logo Municipalidad" class="h-12 w-12 rounded-xl bg-white p-1 object-contain shadow">
                <div>
                    <h1 class="font-extrabold text-xl leading-tight tracking-tight">Municipalidad - Tarja Digital</h1>
                    <p class="text-xs text-green-100 font-medium">Terminal de Autogestión por DNI y Panel Administrador</p>
                </div>
            </div>
            <div class="flex space-x-3">
                <button onclick="cambiarPestana('kiosco')" id="btn-tab-kiosco" class="bg-blue-600 hover:bg-blue-500 text-white ring-2 ring-white px-4 py-2.5 rounded-xl font-bold shadow-md transition">
                    👤 Empleados (Solo DNI)
                </button>
                <button onclick="cambiarPestana('rrhh')" id="btn-tab-rrhh" class="bg-blue-800 hover:bg-blue-700 text-blue-100 px-4 py-2.5 rounded-xl font-semibold shadow transition">
                    🔐 Iniciar Sesión
                </button>
            </div>
        </nav>

        <!-- VISTA 1: TERMINAL EMPLEADOS CON CÁMARA ANTI-TRAMPA -->
        <div id="vista-kiosco" class="max-w-lg mx-auto mt-8 bg-white p-6 rounded-2xl shadow-xl border-t-4 border-green-600 text-center mb-12">
            <h2 class="text-2xl font-extrabold text-slate-800">Terminal de Personal</h2>
            <p class="text-slate-500 text-sm mt-1 mb-4">Ingrese su DNI y mire a la cámara para registrar su asistencia</p>

            <div class="bg-slate-900 text-emerald-400 font-mono text-4xl font-bold py-3 rounded-xl mb-4 shadow-inner" id="reloj-vivo">
                00:00:00
            </div>

            <div class="relative mx-auto w-64 h-48 bg-slate-900 rounded-xl overflow-hidden border-2 border-green-600 shadow mb-3">
                <video id="video-camara" autoplay playsinline muted class="w-full h-full object-cover"></video>
                <canvas id="canvas-foto" width="320" height="240" class="hidden"></canvas>
                <div id="estado-camara" class="absolute bottom-0 inset-x-0 bg-black/70 text-white text-xs py-1">
                    📸 Cámara activa para verificación de identidad
                </div>
            </div>
            <div class="bg-amber-50 border border-amber-300 text-amber-900 text-xs font-semibold px-3 py-2 rounded-lg mb-4">
                ⚠️ Aviso: Al confirmar Entrada o Salida se tomará una foto automática que quedará guardada con su DNI y fecha para auditoría.
            </div>

            <input type="number" id="dni-kiosco" placeholder="Ingrese su DNI sin puntos" autofocus
                class="w-full text-center text-3xl font-bold p-3.5 border-2 border-slate-300 rounded-xl mb-4 focus:border-green-600 focus:outline-none">

            <div class="grid grid-cols-2 gap-4 mb-3">
                <button onclick="registrarFichaje('entrada')" class="bg-green-600 hover:bg-green-700 active:scale-95 transition text-white font-bold py-4 rounded-xl text-lg shadow-lg">
                    🟢 MARCAR ENTRADA
                </button>
                <button onclick="registrarFichaje('salida')" class="bg-orange-500 hover:bg-orange-600 active:scale-95 transition text-white font-bold py-4 rounded-xl text-lg shadow-lg">
                    🟠 MARCAR SALIDA
                </button>
            </div>
            <button onclick="registrarFichaje('consulta')" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 rounded-xl text-md shadow">
                ⏱️ VER MIS HORAS TRABAJADAS HOY Y EN EL MES
            </button>

            <div id="alerta-kiosco" class="mt-5 p-4 rounded-xl hidden text-left"></div>
        </div>

        <!-- VISTA 2: PANEL EXCLUSIVO ADMINISTRADOR -->
        <div id="vista-rrhh" class="max-w-6xl mx-auto mt-8 px-4 hidden pb-16">
            
            <div id="caja-login-rrhh" class="max-w-md mx-auto bg-white p-8 rounded-2xl shadow-xl border-t-4 border-green-700">
                <div class="text-center mb-6">
                    <div class="text-4xl mb-2">🔐</div>
                    <h2 class="text-2xl font-bold text-slate-800">ADMINISTRADOR</h2>
                    <p class="text-slate-500 text-sm">Inicie sesión para auditar asistencias, ver fotos por DNI/fecha, descargar reportes y gestionar la base de datos.</p>
                </div>
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-bold text-slate-700 mb-1">Usuario</label>
                        <input type="text" id="user-rrhh" class="w-full p-3 border rounded-lg focus:outline-green-600" placeholder="Usuario Administrador">
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-700 mb-1">Contraseña</label>
                        <input type="password" id="pass-rrhh" class="w-full p-3 border rounded-lg focus:outline-green-600" placeholder="••••••••">
                    </div>
                    <div id="error-login" class="text-red-600 text-sm font-bold hidden"></div>
                    <button onclick="iniciarSesionRRHH()" class="w-full bg-green-700 hover:bg-green-800 text-white font-bold py-3.5 rounded-xl shadow">
                        INICIAR SESIÓN
                    </button>
                </div>
            </div>

            <!-- PANEL DESBLOQUEADO CON PESTAÑAS INTERNAS -->
            <div id="panel-descargas-rrhh" class="space-y-6 hidden">
                
                <div class="bg-white p-6 rounded-2xl shadow border-l-8 border-green-700 flex flex-wrap justify-between items-center gap-4">
                    <div>
                        <span id="badge-rol" class="bg-green-100 text-green-800 text-xs font-extrabold px-3 py-1 rounded-full uppercase">ADMINISTRADOR</span>
                        <h2 class="text-2xl font-bold text-slate-800 mt-1">Panel de Control General</h2>
                    </div>
                    <div class="flex flex-wrap gap-2">
                        <button onclick="cambiarSubPestanaAdmin('reportes')" id="btn-sub-reportes" class="bg-blue-600 text-white font-bold px-4 py-2.5 rounded-xl text-sm shadow">
                            📊 Reportes de Asistencia
                        </button>
                        <button onclick="cambiarSubPestanaAdmin('personal')" id="btn-sub-personal" class="bg-slate-200 hover:bg-slate-300 text-slate-800 font-bold px-4 py-2.5 rounded-xl text-sm">
                            🪪 Gestionar Personal / Cargar DNI
                        </button>
                        <button onclick="cerrarSesionRRHH()" class="bg-red-600 hover:bg-red-700 text-white font-bold px-4 py-2.5 rounded-xl text-sm shadow">
                            Cerrar Sesión
                        </button>
                    </div>
                </div>

                <!-- SUB-PESTAÑA 1: REPORTES DE ASISTENCIA -->
                <div id="subvista-reportes" class="bg-white p-6 rounded-2xl shadow border">
                    <h3 class="text-xl font-bold text-slate-800 mb-4">📊 Reportes de Asistencia y Liquidación Mensual</h3>
                    
                    <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
                        <button onclick="seleccionarPeriodo('dia')" class="bg-blue-50 hover:bg-blue-100 border border-blue-300 text-blue-900 font-bold py-3 rounded-xl text-sm">
                            📅 Día de Hoy
                        </button>
                        <button onclick="seleccionarPeriodo('semana')" class="bg-green-50 hover:bg-green-100 border border-green-300 text-green-900 font-bold py-3 rounded-xl text-sm">
                            📆 Últimos 7 Días (Semanal)
                        </button>
                        <button onclick="seleccionarPeriodo('mes30')" class="bg-emerald-50 hover:bg-emerald-100 border border-emerald-300 text-emerald-900 font-bold py-3 rounded-xl text-sm">
                            📊 Mensual (Últimos 30 Días)
                        </button>
                        <button onclick="seleccionarPeriodo('mes_anterior')" class="bg-amber-50 hover:bg-amber-100 border border-amber-300 text-amber-900 font-bold py-3 rounded-xl text-sm">
                            ⏪ Mes Anterior Completo
                        </button>
                    </div>

                    <div class="grid grid-cols-1 md:grid-cols-4 gap-4 bg-slate-50 p-4 rounded-xl border mb-5">
                        <div>
                            <label class="block text-xs font-bold text-slate-700 mb-1">SELECCIONAR SECTOR:</label>
                            <select id="filtro-sector" class="w-full p-2.5 border-2 border-green-500 rounded-lg bg-white font-semibold">
                                <option value="">Todos los sectores</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-700 mb-1">BUSCAR POR DNI (OPCIONAL):</label>
                            <input type="text" id="filtro-dni" placeholder="Ej: 41488846" class="w-full p-2.5 border rounded-lg bg-white">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-700 mb-1">FECHA DESDE (O MES PARA HACIENDA):</label>
                            <input type="date" id="fecha-desde" class="w-full p-2.5 border rounded-lg bg-white">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-700 mb-1">FECHA HASTA:</label>
                            <input type="date" id="fecha-hasta" class="w-full p-2.5 border rounded-lg bg-white">
                        </div>
                    </div>

                    <div class="grid grid-cols-1 md:grid-cols-4 gap-3">
                        <button onclick="verTablaEnVivo()" class="bg-blue-700 hover:bg-blue-800 text-white font-bold py-3.5 px-3 rounded-xl shadow text-sm">
                            👁️ VER EN PANTALLA
                        </button>
                        <button onclick="descargarPDFAdmin()" class="bg-rose-600 hover:bg-rose-700 text-white font-bold py-3.5 px-3 rounded-xl shadow text-sm">
                            📄 DESCARGAR PDF
                        </button>
                        <button onclick="descargarExcelDiario()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-bold py-3.5 px-3 rounded-xl shadow text-sm">
                            📥 EXCEL DIARIO (ASISTENCIAS)
                        </button>
                        <button onclick="descargarExcelHacienda()" class="bg-teal-800 hover:bg-teal-900 text-white font-bold py-3.5 px-3 rounded-xl shadow text-sm">
                            🏛️ EXCEL MENSUAL HACIENDA
                        </button>
                    </div>

                    <div id="contenedor-tabla-vivo" class="mt-6 hidden">
                        <div class="overflow-x-auto border rounded-xl">
                            <table class="w-full text-left border-collapse text-sm">
                                <thead class="bg-green-700 text-white">
                                    <tr>
                                        <th class="p-3">Fecha</th>
                                        <th class="p-3">DNI / CUIL</th>
                                        <th class="p-3">Empleado</th>
                                        <th class="p-3">Sector</th>
                                        <th class="p-3">Entrada / Salida (6hs)</th>
                                        <th class="p-3">Salida Real</th>
                                        <th class="p-3">Horas</th>
                                        <th class="p-3 text-center">Auditoría Fotos</th>
                                    </tr>
                                </thead>
                                <tbody id="tbody-vivo" class="divide-y"></tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <!-- SUB-PESTAÑA 2: BASE DE DATOS DE PERSONAL -->
                <div id="subvista-personal" class="bg-white p-6 rounded-2xl shadow border hidden">
                    <div class="flex justify-between items-center mb-3">
                        <div>
                            <h3 class="text-xl font-bold text-slate-800">🪪 Base de Datos de Personal (Agregar / Editar DNI y CUIL)</h3>
                            <p class="text-slate-500 text-sm">Registre nuevos empleados o actualice su DNI, CUIL, Secretaría y Función.</p>
                        </div>
                        <button onclick="cambiarSubPestanaAdmin('reportes')" class="text-sm text-blue-700 font-bold hover:underline">
                            ← Volver a Reportes
                        </button>
                    </div>

                    <div class="grid grid-cols-1 md:grid-cols-4 gap-3 bg-green-50 p-4 rounded-xl border border-green-200 mb-6">
                        <input type="hidden" id="emp-id-edit">
                        <input type="text" id="emp-nombre" placeholder="Apellido y Nombre" class="p-2.5 border rounded-lg bg-white">
                        <input type="number" id="emp-dni" placeholder="Número de DNI (Sin puntos)" class="p-2.5 border rounded-lg bg-white">
                        <input type="number" id="emp-legajo" placeholder="CUIL (Sin guiones ni puntos)" class="p-2.5 border rounded-lg bg-white">
                        <select id="emp-sector" class="p-2.5 border rounded-lg bg-white"></select>
                        <input type="text" id="emp-secretaria" value="GOBIERNO" placeholder="Secretaría (Ej: GOBIERNO)" class="p-2.5 border rounded-lg bg-white">
                        <input type="text" id="emp-funcion" value="ADMINISTRATIVA" placeholder="Función (Ej: ADMINISTRATIVA)" class="p-2.5 border rounded-lg bg-white">
                        <button onclick="guardarEmpleadoWeb()" id="btn-guardar-emp" class="md:col-span-2 bg-green-700 hover:bg-green-800 text-white font-bold py-2.5 px-4 rounded-lg shadow">
                            ➕ Guardar Empleado / DNI
                        </button>
                    </div>

                    <div class="flex justify-between items-center mb-3">
                        <input type="text" id="buscar-emp-web" oninput="filtrarEmpleadosWeb()" placeholder="🔍 Buscar en la base de datos por DNI, Nombre o CUIL..." class="w-full md:w-1/2 p-2.5 border rounded-lg">
                    </div>

                    <div class="overflow-x-auto border rounded-xl max-h-96">
                        <table class="w-full text-left border-collapse text-sm">
                            <thead class="bg-slate-200 text-slate-700 sticky top-0">
                                <tr>
                                    <th class="p-3">DNI</th>
                                    <th class="p-3">CUIL</th>
                                    <th class="p-3">Nombre Completo</th>
                                    <th class="p-3">Secretaría / Sector</th>
                                    <th class="p-3">Función</th>
                                    <th class="p-3 text-center">Acción</th>
                                </tr>
                            </thead>
                            <tbody id="tbody-empleados" class="divide-y"></tbody>
                        </table>
                    </div>
                </div>

            </div>
        </div>

        <div id="modal-foto" onclick="cerrarModalFoto()" class="fixed inset-0 bg-black/80 hidden items-center justify-center z-50 p-4">
            <div class="bg-white p-5 rounded-2xl max-w-md w-full text-center shadow-2xl" onclick="event.stopPropagation()">
                <h4 id="titulo-modal-foto" class="font-bold text-lg text-slate-800 mb-1"></h4>
                <p id="subtitulo-modal-foto" class="text-xs text-slate-500 mb-3"></p>
                <img id="img-modal-foto" src="" class="w-full rounded-xl border-2 border-green-600 mb-4">
                <button onclick="cerrarModalFoto()" class="bg-green-700 text-white font-bold px-6 py-2 rounded-lg">Cerrar</button>
            </div>
        </div>

        <script>
            let tokenAdmin = localStorage.getItem('tarja_rrhh_token') || null;
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
                
                document.getElementById('btn-tab-kiosco').className = esKiosco 
                    ? "bg-blue-600 hover:bg-blue-500 text-white ring-2 ring-white px-4 py-2.5 rounded-xl font-bold shadow-md transition"
                    : "bg-blue-800 hover:bg-blue-700 text-blue-100 px-4 py-2.5 rounded-xl font-semibold shadow transition";
                document.getElementById('btn-tab-rrhh').className = !esKiosco 
                    ? "bg-blue-600 hover:bg-blue-500 text-white ring-2 ring-white px-4 py-2.5 rounded-xl font-bold shadow-md transition"
                    : "bg-blue-800 hover:bg-blue-700 text-blue-100 px-4 py-2.5 rounded-xl font-semibold shadow transition";

                if (!esKiosco) verificarEstadoLogin();
            }

            function cambiarSubPestanaAdmin(sub) {
                const esReportes = (sub === 'reportes');
                document.getElementById('subvista-reportes').classList.toggle('hidden', !esReportes);
                document.getElementById('subvista-personal').classList.toggle('hidden', esReportes);

                document.getElementById('btn-sub-reportes').className = esReportes
                    ? "bg-blue-600 text-white font-bold px-4 py-2.5 rounded-xl text-sm shadow"
                    : "bg-slate-200 hover:bg-slate-300 text-slate-800 font-bold px-4 py-2.5 rounded-xl text-sm";
                document.getElementById('btn-sub-personal').className = !esReportes
                    ? "bg-blue-600 text-white font-bold px-4 py-2.5 rounded-xl text-sm shadow"
                    : "bg-slate-200 hover:bg-slate-300 text-slate-800 font-bold px-4 py-2.5 rounded-xl text-sm";
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

                const foto = (tipo === 'entrada' || tipo === 'salida') ? capturarFotoBase64() : null;

                try {
                    const res = await fetch('/kiosco/fichar', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({dni: dni, tipo: tipo, foto_base64: foto})
                    });
                    const data = await res.json();

                    if (res.ok) {
                        alerta.className = "mt-5 p-5 rounded-xl bg-emerald-100 text-emerald-950 border-2 border-emerald-400 block";
                        alerta.innerHTML = `
                            <div class="text-xl font-extrabold">${data.empleado}</div>
                            <div class="text-xs uppercase text-emerald-700 font-bold mb-2">Sector: ${data.sector}</div>
                            <div class="font-bold text-lg">${data.mensaje}</div>
                            <div class="mt-1 text-sm bg-white/80 p-2.5 rounded border border-emerald-300 font-semibold">${data.submensaje}</div>
                        `;
                        if (tipo !== 'consulta') input.value = "";
                    } else {
                        alerta.className = "mt-5 p-5 rounded-xl bg-red-100 text-red-900 border-2 border-red-300 block font-bold";
                        alerta.innerText = "❌ " + (data.detail || "No se pudo procesar.");
                    }
                } catch (err) {
                    alerta.className = "mt-5 p-5 rounded-xl bg-red-100 text-red-900 block font-bold";
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
                    const res = await fetch('/login', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: formData
                    });
                    const data = await res.json();

                    if (res.ok) {
                        if (data.rol !== 'admin' && data.rol !== 'rrhh') {
                            errBox.innerText = "Acceso denegado: Se requieren permisos de ADMINISTRADOR.";
                            errBox.classList.remove('hidden');
                            return;
                        }
                        tokenAdmin = data.access_token;
                        localStorage.setItem('tarja_rrhh_token', tokenAdmin);
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
                verificarEstadoLogin();
            }

            async function verificarEstadoLogin() {
                const loginBox = document.getElementById('caja-login-rrhh');
                const panelBox = document.getElementById('panel-descargas-rrhh');

                if (!tokenAdmin) {
                    loginBox.classList.remove('hidden');
                    panelBox.classList.add('hidden');
                    return;
                }

                const res = await fetch('/admin/sectores', {
                    headers: {'Authorization': 'Bearer ' + tokenAdmin}
                });

                if (res.ok) {
                    const sectores = await res.json();
                    const selectFiltro = document.getElementById('filtro-sector');
                    const selectEmp = document.getElementById('emp-sector');
                    selectFiltro.innerHTML = '<option value="">Todos los sectores</option>';
                    selectEmp.innerHTML = '';
                    sectores.forEach(s => {
                        selectFiltro.innerHTML += `<option value="${s.id}">${s.nombre}</option>`;
                        selectEmp.innerHTML += `<option value="${s.id}">${s.nombre}</option>`;
                    });
                    seleccionarPeriodo('dia');
                    cargarEmpleadosWeb();
                    loginBox.classList.add('hidden');
                    panelBox.classList.remove('hidden');
                } else {
                    cerrarSesionRRHH();
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

                let url = `/admin/asistencias/${desde}?hasta=${hasta}`;
                if (secFiltro) url += `&sector_id=${secFiltro}`;
                if (dniFiltro) url += `&dni=${encodeURIComponent(dniFiltro)}`;

                const res = await fetch(url, { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
                if (res.ok) {
                    const datos = await res.json();
                    const tbody = document.getElementById('tbody-vivo');
                    tbody.innerHTML = datos.length === 0 
                        ? '<tr><td colspan="8" class="p-4 text-center text-slate-500">No hay asistencias registradas en ese rango de fechas.</td></tr>' 
                        : '';
                    
                    datos.sort((a, b) => (a.sector || '').localeCompare(b.sector || ''));
                    let sectorActual = null;

                    datos.forEach(a => {
                        if (a.sector !== sectorActual) {
                            sectorActual = a.sector;
                            tbody.innerHTML += `
                                <tr class="bg-green-100/80">
                                    <td colspan="8" class="p-2.5 font-extrabold text-green-900 uppercase tracking-wide text-xs">
                                        📂 SECTOR: ${sectorActual}
                                    </td>
                                </tr>
                            `;
                        }

                        const btnFotoIn = a.foto_llegada 
                            ? `<button onclick="verFotoGrande('${a.foto_llegada}', '${a.nombre}', '${a.dni}', '${a.fecha}', 'Entrada: ${a.hora_llegada}')" class="bg-emerald-100 text-emerald-800 px-2 py-1 rounded text-xs font-bold hover:bg-emerald-200 mr-1">📸 Entrada</button>`
                            : `<span class="text-xs text-slate-400 mr-1">Sin foto</span>`;
                        const btnFotoOut = a.foto_salida 
                            ? `<button onclick="verFotoGrande('${a.foto_salida}', '${a.nombre}', '${a.dni}', '${a.fecha}', 'Salida: ${a.hora_salida}')" class="bg-orange-100 text-orange-800 px-2 py-1 rounded text-xs font-bold hover:bg-orange-200">📸 Salida</button>`
                            : ``;

                        tbody.innerHTML += `
                            <tr class="hover:bg-slate-50">
                                <td class="p-3 font-mono text-xs">${a.fecha}</td>
                                <td class="p-3 font-mono"><b>${a.dni || 'Sin DNI'}</b><br><span class="text-xs text-slate-500">CUIL: ${a.legajo || '-'}</span></td>
                                <td class="p-3 font-bold">${a.nombre}</td>
                                <td class="p-3 text-xs font-semibold text-green-800">${a.sector}</td>
                                <td class="p-3"><span class="text-emerald-700 font-bold">${a.hora_llegada || '-'}</span><br><span class="text-xs text-slate-500">Sale 6hs: ${a.salida_estimada || '-'}</span></td>
                                <td class="p-3 text-orange-600 font-semibold">${a.hora_salida || (a.en_curso ? 'Trabajando...' : '-')}</td>
                                <td class="p-3 font-bold ${a.en_curso ? 'text-blue-700' : 'text-emerald-800'}">${a.estado !== 'Presente' ? a.estado : a.tiempo_trabajado}</td>
                                <td class="p-3 text-center">${btnFotoIn}${btnFotoOut}</td>
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

                let url = `/admin/reporte_pdf?fecha=${desde}&hasta=${hasta}`;
                if (sec) url += `&sector_id=${sec}`;
                if (dni) url += `&dni=${encodeURIComponent(dni)}`;

                const res = await fetch(url, { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
                if (res.ok) {
                    const blob = await res.blob();
                    const link = document.createElement('a');
                    link.href = window.URL.createObjectURL(blob);
                    link.download = `Reporte_Tarja_${desde}_al_${hasta}.pdf`;
                    link.click();
                } else {
                    const err = await res.json();
                    alert(err.detail || "No hay datos para generar el PDF.");
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

                let url = `/rrhh/exportar-diario?desde=${d}&hasta=${h}`;
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

                let url = `/rrhh/exportar-mensual-hacienda?desde=${d}`;
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
                const res = await fetch('/admin/empleados', { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
                if (res.ok) {
                    listaEmpleadosGlobal = await res.json();
                    renderizarEmpleadosWeb(listaEmpleadosGlobal);
                }
            }

            function renderizarEmpleadosWeb(lista) {
                const tbody = document.getElementById('tbody-empleados');
                tbody.innerHTML = '';
                lista.forEach(emp => {
                    const cuilLimpio = (emp.legajo || '').replace(/[^0-9]/g, '');
                    tbody.innerHTML += `
                        <tr class="hover:bg-slate-50">
                            <td class="p-3 font-mono font-bold ${emp.dni ? 'text-green-800' : 'text-red-500'}">${emp.dni || 'Falta DNI'}</td>
                            <td class="p-3 font-mono">${cuilLimpio}</td>
                            <td class="p-3 font-semibold">${emp.nombre_completo}</td>
                            <td class="p-3 text-xs"><b>${emp.secretaria || 'GOBIERNO'}</b><br>${emp.sector_nombre}</td>
                            <td class="p-3 text-xs">${emp.funcion || 'ADMINISTRATIVA'}</td>
                            <td class="p-3 text-center">
                                <button onclick='cargarParaEditar(${JSON.stringify(emp)})' class="text-blue-600 hover:underline font-bold text-xs">✏️ Editar</button>
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
                    (e.legajo || '').toLowerCase().includes(q)
                );
                renderizarEmpleadosWeb(filtrados);
            }

            function cargarParaEditar(emp) {
                document.getElementById('emp-id-edit').value = emp.id;
                document.getElementById('emp-nombre').value = emp.nombre_completo;
                document.getElementById('emp-dni').value = emp.dni || '';
                document.getElementById('emp-legajo').value = (emp.legajo || '').replace(/[^0-9]/g, '');
                document.getElementById('emp-sector').value = emp.sector_id;
                document.getElementById('emp-secretaria').value = emp.secretaria || 'GOBIERNO';
                document.getElementById('emp-funcion').value = emp.funcion || 'ADMINISTRATIVA';
                document.getElementById('btn-guardar-emp').innerText = "💾 Actualizar Empleado";
            }

            async function guardarEmpleadoWeb() {
                const idEdit = document.getElementById('emp-id-edit').value;
                const nombre = document.getElementById('emp-nombre').value.trim();
                const dni = document.getElementById('emp-dni').value.trim().replace(/[^0-9]/g, '');
                const legajo = document.getElementById('emp-legajo').value.trim().replace(/[^0-9]/g, '');
                const sector_id = parseInt(document.getElementById('emp-sector').value);
                const secretaria = document.getElementById('emp-secretaria').value.trim() || 'GOBIERNO';
                const funcion = document.getElementById('emp-funcion').value.trim() || 'ADMINISTRATIVA';

                if (!nombre || !dni || !legajo || !sector_id) {
                    alert("Complete Nombre, DNI, CUIL y Sector.");
                    return;
                }

                const url = idEdit ? `/admin/empleados/${idEdit}` : '/admin/empleados';
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
                    document.getElementById('btn-guardar-emp').innerText = "➕ Guardar Empleado / DNI";
                    cargarEmpleadosWeb();
                } else {
                    alert("Error: " + (data.detail || "No se pudo guardar"));
                }
            }
        </script>
    </body>
    </html>
    """