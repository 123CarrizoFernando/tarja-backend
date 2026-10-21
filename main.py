from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import Response, HTMLResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import extract, func
from datetime import date, datetime, timedelta, timezone
from passlib.context import CryptContext
from jose import JWTError, jwt
from typing import List

import base64
import requests
import os
import io
import csv

from pydantic import BaseModel
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet

import models
import schemas
from database import engine, get_db

# Crea las tablas en la base de datos si no existen
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Tarja Backend API")

# --- CONFIGURACIÓN DE SEGURIDAD (JWT) ---
SECRET_KEY = "tu_super_clave_secreta_aqui"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 1 semana

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

ZONA_ARG = timezone(timedelta(hours=-3))

def obtener_ahora_arg() -> datetime:
    return datetime.now(ZONA_ARG)

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# --- DEPENDENCIA DE AUTENTICACIÓN ---
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
        
        # Si entra 07:10, le sumamos 6 horas -> debe salir 13:10
        dt_salida_estimada = dt_ingreso + timedelta(hours=horas_exigidas)
        salida_estimada_str = dt_salida_estimada.strftime("%H:%M")
        
        en_curso = False
        if hora_salida_str:
            h_out, m_out = map(int, hora_salida_str.split(":"))
            dt_fin = ahora.replace(hour=h_out, minute=m_out, second=0, microsecond=0)
        else:
            # Si todavía no marcó salida y el registro es de hoy, calculamos en tiempo real
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

# --- RUTAS DE LA API ---

@app.post("/sectores/", response_model=schemas.Sector)
def crear_sector(sector: schemas.SectorCreate, db: Session = Depends(get_db)):
    db_sector = models.Sector(nombre=sector.nombre)
    db.add(db_sector)
    db.commit()
    db.refresh(db_sector)
    return db_sector

@app.post("/encargados/", response_model=schemas.Encargado)
def crear_encargado(encargado: schemas.EncargadoCreate, db: Session = Depends(get_db)):
    db_usuario = db.query(models.Encargado).filter(models.Encargado.usuario == encargado.usuario).first()
    if db_usuario:
        raise HTTPException(status_code=400, detail="El usuario ya está registrado")
    
    hashed_password = get_password_hash(encargado.password)
    nuevo_encargado = models.Encargado(
        usuario=encargado.usuario,
        password_hash=hashed_password,
        sector_id=encargado.sector_id
    )
    db.add(nuevo_encargado)
    db.commit()
    db.refresh(nuevo_encargado)
    return nuevo_encargado

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
        "rol": usuario.rol
    }

# ==========================================
# ESQUEMAS ADICIONALES
# ==========================================
class SectorNuevo(BaseModel):
    nombre: str

class EmpleadoNuevo(BaseModel):
    dni: str
    nombre_completo: str
    legajo: str
    sector_id: int

class FichajeKioscoRequest(BaseModel):
    dni: str
    tipo: str # "entrada" o "salida"

# ==========================================
# CANDADO DE SEGURIDAD PARA EL ADMINISTRADOR
# ==========================================
def obtener_admin_actual(usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    if usuario_actual.rol != "admin":
        raise HTTPException(
            status_code=403, 
            detail="Acceso denegado: Se requieren permisos de Administrador."
        )
    return usuario_actual

# ==========================================
# RUTAS CRUD - EXCLUSIVAS DEL ADMINISTRADOR
# ==========================================

@app.get("/admin/sectores")
def ver_sectores_admin(db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    return db.query(models.Sector).all()

@app.get("/admin/empleados")
def ver_todos_los_empleados_admin(db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    empleados = db.query(models.Empleado).filter(models.Empleado.activo == True).all()
    resultado = []
    for emp in empleados:
        resultado.append({
            "id": emp.id,
            "dni": emp.dni,
            "nombre_completo": emp.nombre_completo,
            "legajo": emp.legajo,
            "sector_id": emp.sector_id,
            "sector_nombre": emp.sector.nombre if emp.sector else "Sin sector"
        })
    return resultado

@app.post("/admin/sectores")
def crear_sector_admin(sector: SectorNuevo, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    existe = db.query(models.Sector).filter(models.Sector.nombre == sector.nombre).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ese sector ya existe en la base de datos.")
    
    nuevo_sector = models.Sector(nombre=sector.nombre)
    db.add(nuevo_sector)
    db.commit()
    db.refresh(nuevo_sector)
    return {"mensaje": "Sector creado con éxito", "sector": nuevo_sector}

@app.post("/admin/empleados")
def crear_empleado_admin(empleado: EmpleadoNuevo, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    existe = db.query(models.Empleado).filter(
        (models.Empleado.dni == empleado.dni) | (models.Empleado.legajo == empleado.legajo)
    ).first()
    
    if existe:
        raise HTTPException(status_code=400, detail="El DNI o Legajo ya está registrado.")

    nuevo_empleado = models.Empleado(
        dni=empleado.dni,
        nombre_completo=empleado.nombre_completo,
        legajo=empleado.legajo,
        sector_id=empleado.sector_id,
        activo=True
    )
    db.add(nuevo_empleado)
    db.commit()
    return {"mensaje": f"Empleado {empleado.nombre_completo} creado con éxito"}

@app.put("/admin/empleados/{empleado_id}")
def editar_empleado_admin(empleado_id: int, datos: EmpleadoNuevo, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    empleado = db.query(models.Empleado).filter(models.Empleado.id == empleado_id).first()
    if not empleado:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    
    duplicado = db.query(models.Empleado).filter(
        models.Empleado.id != empleado_id,
        (models.Empleado.dni == datos.dni) | (models.Empleado.legajo == datos.legajo)
    ).first()
    
    if duplicado:
        raise HTTPException(status_code=400, detail="El DNI o Legajo ya pertenece a otro empleado.")
    
    empleado.nombre_completo = datos.nombre_completo
    empleado.dni = datos.dni
    empleado.legajo = datos.legajo
    empleado.sector_id = datos.sector_id
    
    db.commit()
    return {"mensaje": "Empleado actualizado correctamente."}

@app.put("/admin/empleados/{empleado_id}/baja")
def baja_empleado(empleado_id: int, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    empleado = db.query(models.Empleado).filter(models.Empleado.id == empleado_id).first()
    if not empleado:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    
    empleado.activo = False
    db.commit()
    return {"mensaje": f"El empleado {empleado.nombre_completo} ha sido dado de baja."}

@app.put("/admin/empleados/{empleado_id}/sector/{nuevo_sector_id}")
def cambiar_sector_empleado(empleado_id: int, nuevo_sector_id: int, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    empleado = db.query(models.Empleado).filter(models.Empleado.id == empleado_id).first()
    if not empleado:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    
    empleado.sector_id = nuevo_sector_id
    db.commit()
    return {"mensaje": f"Sector de {empleado.nombre_completo} actualizado correctamente."}

# --- SÚPER TARJA CON CÁLCULO DE HORAS EN VIVO ---
@app.get("/admin/asistencias/{fecha_solicitada}")
def obtener_todas_asistencias(fecha_solicitada: date, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    asistencias = db.query(models.Asistencia).filter(models.Asistencia.fecha == fecha_solicitada).all()
    
    resultado = []
    for asis in asistencias:
        estado_real = asis.estado if asis.estado else "Presente"
        calc = calcular_tiempos_jornada(asis.hora_llegada, asis.hora_salida, fecha_solicitada)
        
        resultado.append({
            "empleado_id": asis.empleado_id,
            "dni": asis.empleado.dni,
            "nombre": asis.empleado.nombre_completo,
            "legajo": asis.empleado.legajo,
            "sector_id": asis.empleado.sector_id,
            "sector": asis.empleado.sector.nombre if asis.empleado.sector else "Sin sector",
            "hora_llegada": asis.hora_llegada,
            "hora_salida": asis.hora_salida,
            "salida_estimada": calc["salida_estimada"],
            "tiempo_trabajado": calc["trabajado_str"],
            "en_curso": calc["en_curso"] and (estado_real == "Presente"),
            "cumplio_jornada": calc["cumplio_jornada"],
            "estado": estado_real
        })
    return resultado

class EncargadoNuevoAdmin(BaseModel):
    usuario: str
    password: str
    sector_id: int

@app.get("/admin/encargados")
def ver_encargados_admin(db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    encargados = db.query(models.Encargado).all()
    resultado = []
    for enc in encargados:
        resultado.append({
            "id": enc.id,
            "usuario": enc.usuario,
            "rol": enc.rol,
            "sector_nombre": enc.sector.nombre if enc.sector else "Sin sector"
        })
    return resultado

@app.post("/admin/encargados")
def crear_encargado_admin(datos: EncargadoNuevoAdmin, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    existe = db.query(models.Encargado).filter(models.Encargado.usuario == datos.usuario).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ese nombre de usuario ya existe.")
    
    nuevo_encargado = models.Encargado(
        usuario=datos.usuario,
        password_hash=get_password_hash(datos.password),
        sector_id=datos.sector_id,
        rol="encargado"
    )
    db.add(nuevo_encargado)
    db.commit()
    return {"mensaje": f"Usuario {datos.usuario} creado con éxito."}

@app.get("/admin/dashboard")
def obtener_dashboard_admin(db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    hoy = obtener_ahora_arg().date()
    
    total_empleados = db.query(models.Empleado).filter(models.Empleado.activo == True).count()
    asistencias_hoy = db.query(models.Asistencia).filter(models.Asistencia.fecha == hoy).all()
    
    presentes = 0
    faltas = 0
    licencias = 0
    
    for asis in asistencias_hoy:
        estado_real = asis.estado if asis.estado else "Presente"
        if estado_real == "Falta":
            faltas += 1
        elif estado_real == "Licencia":
            licencias += 1
        else:
            presentes += 1
            
    sin_marcar = total_empleados - (presentes + faltas + licencias)
    if sin_marcar < 0: sin_marcar = 0
    
    primer_dia_mes = hoy.replace(day=1)
    
    ranking = db.query(
        models.Sector.nombre,
        func.count(models.Asistencia.id).label('total_faltas')
    ).select_from(models.Asistencia).join(models.Empleado).join(models.Sector).filter(
        models.Asistencia.estado == 'Falta',
        models.Asistencia.fecha >= primer_dia_mes,
        models.Asistencia.fecha <= hoy
    ).group_by(models.Sector.nombre).order_by(func.count(models.Asistencia.id).desc()).limit(3).all()
    
    ranking_formateado = [{"sector": r[0], "faltas": r[1]} for r in ranking]
    
    return {
        "hoy": {
            "total": total_empleados,
            "presentes": presentes,
            "faltas": faltas,
            "licencias": licencias,
            "sin_marcar": sin_marcar
        },
        "ranking_mes": ranking_formateado
    }

# ---------------------------------------------------------
# RUTAS DE LA APP MÓVIL (ENCARGADOS)
# ---------------------------------------------------------
@app.get("/empleados")
def obtener_empleados_sector(db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    empleados = db.query(models.Empleado).filter(
        models.Empleado.sector_id == usuario_actual.sector_id,
        models.Empleado.activo == True
    ).all()
    return empleados

@app.post("/asistencias/sincronizar")
def sincronizar_asistencias(asistencias: List[schemas.AsistenciaCreate], db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    for asis in asistencias:
        registro_existente = db.query(models.Asistencia).filter(
            models.Asistencia.empleado_id == asis.empleado_id,
            models.Asistencia.fecha == asis.fecha
        ).first()

        if registro_existente:
            if asis.hora_llegada:
                registro_existente.hora_llegada = asis.hora_llegada
            if asis.hora_salida:
                registro_existente.hora_salida = asis.hora_salida
        else:
            nueva_asistencia = models.Asistencia(
                empleado_id=asis.empleado_id,
                fecha=asis.fecha,
                hora_llegada=asis.hora_llegada,
                hora_salida=asis.hora_salida,
                estado="Presente"
            )
            db.add(nueva_asistencia)
            
    db.commit()
    return {"mensaje": f"Se sincronizaron {len(asistencias)} registros con éxito."}

@app.get("/asistencias/hoy")
def obtener_asistencias_hoy(db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    hoy = obtener_ahora_arg().date()
    empleados_sector = db.query(models.Empleado).filter(models.Empleado.sector_id == usuario_actual.sector_id).all()
    ids_empleados = [emp.id for emp in empleados_sector]

    asistencias_hoy = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id.in_(ids_empleados),
        models.Asistencia.fecha == hoy
    ).all()
    return asistencias_hoy

# ==========================================
#        REPORTES Y CIERRE DE JORNADA
# ==========================================
class CorreoRequest(BaseModel):
    correo_destino: str

@app.get("/reporte/descargar_pdf")
def descargar_pdf(db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    hoy = obtener_ahora_arg().date()

    sector = db.query(models.Sector).filter(models.Sector.id == usuario_actual.sector_id).first()
    nombre_sector = sector.nombre if sector else f"Sector {usuario_actual.sector_id}"
    
    empleados_sector = db.query(models.Empleado).filter(models.Empleado.sector_id == usuario_actual.sector_id).all()
    ids_empleados = [emp.id for emp in empleados_sector]

    if not ids_empleados:
        raise HTTPException(status_code=404, detail="No hay empleados asignados a tu sector.")

    asistencias_hoy = db.query(models.Asistencia).filter(
        models.Asistencia.fecha == hoy,
        models.Asistencia.empleado_id.in_(ids_empleados)
    ).all()
    
    if not asistencias_hoy:
        raise HTTPException(status_code=404, detail="No hay registros hoy para tu sector. Sincronice primero.")

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4)
    elementos = []
    estilos = getSampleStyleSheet()

    ruta_logo = "logo_muni.png"
    if os.path.exists(ruta_logo):
        imagen_logo = Image(ruta_logo, width=500, height=130)
        elementos.append(imagen_logo)
        elementos.append(Spacer(1, 15))

    titulo = Paragraph(f"Reporte de Asistencia ({nombre_sector}) - {hoy.strftime('%d/%m/%Y')}", estilos['Title'])
    elementos.append(titulo)
    elementos.append(Spacer(1, 20))

    datos_tabla = [["Legajo", "Nombre", "Llegada", "Salida", "Horas"]]
    empleados_dict = {emp.id: emp for emp in empleados_sector}
    
    for asis in asistencias_hoy:
        empleado = empleados_dict.get(asis.empleado_id)
        if empleado:
            if asis.estado and asis.estado != "Presente":
                llegada = asis.estado
                salida = asis.estado
                trabajado = "00h 00m"
            else:
                llegada = asis.hora_llegada if asis.hora_llegada else "No marcó"
                salida = asis.hora_salida if asis.hora_salida else "En curso"
                calc = calcular_tiempos_jornada(asis.hora_llegada, asis.hora_salida, hoy)
                trabajado = calc["trabajado_str"]
            datos_tabla.append([empleado.legajo, empleado.nombre_completo, llegada, salida, trabajado])

    tabla = Table(datos_tabla, colWidths=[65, 185, 70, 70, 75])
    estilo_tabla = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#ecf0f1')),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ])
    tabla.setStyle(estilo_tabla)
    elementos.append(tabla)

    pdf.build(elementos)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    nombre_archivo = f"Asistencia_{nombre_sector.replace(' ', '_')}_{hoy.strftime('%Y%m%d')}.pdf"
    
    return Response(
        content=pdf_bytes, 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"}
    )

API_KEY_RESEND = os.environ.get("RESEND_API_KEY") 

@app.post("/reporte/enviar_correo_api")
def enviar_correo_api(request: CorreoRequest, db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    hoy = obtener_ahora_arg().date()
    
    empleados_sector = db.query(models.Empleado).filter(models.Empleado.sector_id == usuario_actual.sector_id).all()
    ids_empleados = [emp.id for emp in empleados_sector]

    if not ids_empleados:
        raise HTTPException(status_code=404, detail="No hay empleados en tu sector.")

    asistencias_hoy = db.query(models.Asistencia).filter(
        models.Asistencia.fecha == hoy,
        models.Asistencia.empleado_id.in_(ids_empleados)
    ).all()
    
    if not asistencias_hoy:
        raise HTTPException(status_code=404, detail="No hay registros hoy para tu sector. Sincronice primero.")

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4)
    elementos = []
    estilos = getSampleStyleSheet()

    titulo = Paragraph(f"Reporte de Asistencia (Sector {usuario_actual.sector_id}) - {hoy.strftime('%d/%m/%Y')}", estilos['Title'])
    elementos.append(titulo)
    elementos.append(Spacer(1, 20))

    datos_tabla = [["Legajo", "Nombre", "Llegada", "Salida", "Horas"]]
    empleados_dict = {emp.id: emp for emp in empleados_sector}
    
    for asis in asistencias_hoy:
        empleado = empleados_dict.get(asis.empleado_id)
        if empleado:
            if asis.estado and asis.estado != "Presente":
                llegada = asis.estado
                salida = asis.estado
                trabajado = "00h 00m"
            else:
                llegada = asis.hora_llegada if asis.hora_llegada else "No marcó"
                salida = asis.hora_salida if asis.hora_salida else "En curso"
                calc = calcular_tiempos_jornada(asis.hora_llegada, asis.hora_salida, hoy)
                trabajado = calc["trabajado_str"]
            datos_tabla.append([empleado.legajo, empleado.nombre_completo, llegada, salida, trabajado])

    tabla = Table(datos_tabla, colWidths=[65, 185, 70, 70, 75])
    tabla.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#ecf0f1')),
    ]))
    elementos.append(tabla)

    pdf.build(elementos)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
    url_resend = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {API_KEY_RESEND}",
        "Content-Type": "application/json"
    }
    payload = {
        "from": "Tarja Obras <onboarding@resend.dev>",
        "to": [request.correo_destino],
        "subject": f"Cierre de Jornada Tarja - Sector {usuario_actual.sector_id} ({hoy.strftime('%d/%m/%Y')})",
        "html": f"<p>Adjunto encontrarás el reporte de asistencias exclusivo del Sector {usuario_actual.sector_id}.</p>",
        "attachments": [
            {
                "filename": f"Asistencia_Sector_{usuario_actual.sector_id}_{hoy.strftime('%Y%m%d')}.pdf",
                "content": pdf_base64
            }
        ]
    }

    respuesta = requests.post(url_resend, headers=headers, json=payload)
    if respuesta.status_code in [200, 201]:
        return {"mensaje": f"Reporte del sector enviado con éxito a {request.correo_destino}"}
    else:
        raise HTTPException(status_code=500, detail=f"Error en API Resend: {respuesta.text}")

@app.get("/reporte/rango_pdf")
def reporte_rango_pdf(fecha_inicio: date, fecha_fin: date, db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    ahora_arg = obtener_ahora_arg()
    fecha_emision = ahora_arg.strftime('%d/%m/%Y a las %H:%M hs')

    sector_info = db.query(models.Sector).filter(models.Sector.id == usuario_actual.sector_id).first()
    nombre_sector = sector_info.nombre if sector_info else f"Sector {usuario_actual.sector_id}"

    empleados_sector = db.query(models.Empleado).filter(models.Empleado.sector_id == usuario_actual.sector_id).all()
    ids_empleados = [emp.id for emp in empleados_sector]

    if not ids_empleados:
        raise HTTPException(status_code=404, detail="No hay empleados asignados.")

    asistencias_rango = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id.in_(ids_empleados),
        models.Asistencia.fecha >= fecha_inicio,
        models.Asistencia.fecha <= fecha_fin
    ).all()

    if not asistencias_rango:
        raise HTTPException(status_code=404, detail=f"No hay registros entre el {fecha_inicio.strftime('%d/%m/%Y')} y el {fecha_fin.strftime('%d/%m/%Y')}.")

    minutos_por_empleado = {emp.id: 0 for emp in empleados_sector}
    
    for asis in asistencias_rango:
        estado_real = asis.estado if asis.estado else "Presente"
        if asis.hora_llegada and asis.hora_salida and estado_real == "Presente":
            try:
                formato = "%H:%M"
                llegada = datetime.strptime(asis.hora_llegada, formato)
                salida = datetime.strptime(asis.hora_salida, formato)
                
                diferencia = salida - llegada
                minutos_trabajados = diferencia.total_seconds() / 60
                
                if minutos_trabajados > 0:
                    minutos_por_empleado[asis.empleado_id] += minutos_trabajados
            except ValueError:
                pass

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4)
    elementos = []
    estilos = getSampleStyleSheet()

    ruta_logo = "logo_muni.png"
    if os.path.exists(ruta_logo):
        imagen_logo = Image(ruta_logo, width=500, height=130)
        elementos.append(imagen_logo)
        elementos.append(Spacer(1, 15))

    texto_inicio = fecha_inicio.strftime('%d/%m/%Y')
    texto_fin = fecha_fin.strftime('%d/%m/%Y')
    
    titulo_html = (
        f"Total de Horas Trabajadas<br/>"
        f"Sector: {nombre_sector}<br/>"
        f"(Del {texto_inicio} al {texto_fin})<br/>"
        f"<font size=10 color=gray>Generado el: {fecha_emision}</font>"
    )
    
    titulo = Paragraph(titulo_html, estilos['Title'])
    elementos.append(titulo)
    elementos.append(Spacer(1, 20))

    datos_tabla = [["Legajo", "Nombre", "Total de Horas Trabajadas"]]
    empleados_dict = {emp.id: emp for emp in empleados_sector}

    for emp_id, minutos_totales in minutos_por_empleado.items():
        empleado = empleados_dict[emp_id]
        horas = int(minutos_totales // 60)
        minutos_restantes = int(minutos_totales % 60)
        texto_tiempo = f"{horas} hs {minutos_restantes} min" if minutos_totales > 0 else "Sin registros completados"
        datos_tabla.append([empleado.legajo, empleado.nombre_completo, texto_tiempo])

    tabla = Table(datos_tabla, colWidths=[80, 200, 160])
    tabla.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#ecf0f1')),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    elementos.append(tabla)

    pdf.build(elementos)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    nombre_archivo_limpio = nombre_sector.replace(" ", "_")
    nombre_archivo = f"Reporte_{nombre_archivo_limpio}_{texto_inicio.replace('/','-')}_al_{texto_fin.replace('/','-')}.pdf"
    
    return Response(
        content=pdf_bytes, 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"}
    )

def calcular_diferencia_horas(llegada: str, salida: str) -> float:
    if not llegada or not salida:
        return 0.0
    try:
        formato = "%H:%M"
        t_llegada = datetime.strptime(llegada, formato)
        t_salida = datetime.strptime(salida, formato)
        diferencia = t_salida - t_llegada
        horas = diferencia.total_seconds() / 3600.0
        return round(max(0.0, horas), 2)
    except Exception:
        return 0.0

@app.get("/empleados/{empleado_id}/historial")
def obtener_historial_empleado(empleado_id: int, fecha_inicio: date, fecha_fin: date, db: Session = Depends(get_db)):
    empleado = db.query(models.Empleado).filter(models.Empleado.id == empleado_id).first()
    if not empleado:
        raise HTTPException(status_code=404, detail="Empleado no existe en la BD")

    asistencias = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id == empleado_id,
        models.Asistencia.fecha >= fecha_inicio,
        models.Asistencia.fecha <= fecha_fin
    ).order_by(models.Asistencia.fecha).all()

    total_horas = 0.0
    detalle = []

    for asis in asistencias:
        estado_real = asis.estado if asis.estado else "Presente"
        if estado_real == "Presente":
            horas_dia = calcular_diferencia_horas(asis.hora_llegada, asis.hora_salida)
            total_horas += horas_dia
            detalle.append({
                "fecha": asis.fecha,
                "llegada": asis.hora_llegada,
                "salida": asis.hora_salida,
                "horas_trabajadas": horas_dia,
                "estado": estado_real
            })
        else:
            detalle.append({
                "fecha": asis.fecha,
                "llegada": "-",
                "salida": "-",
                "horas_trabajadas": 0.0,
                "estado": estado_real
            })

    return {
        "empleado": empleado.nombre_completo,
        "legajo": empleado.legajo,
        "total_horas": round(total_horas, 2),
        "detalle": detalle
    }

@app.put("/asistencias/{empleado_id}/estado")
def registrar_estado_especial(empleado_id: int, estado: str, db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    hoy = obtener_ahora_arg().date()

    asistencia = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id == empleado_id,
        models.Asistencia.fecha == hoy
    ).first()

    if not asistencia:
        asistencia = models.Asistencia(
            empleado_id=empleado_id,
            fecha=hoy,
            estado=estado
        )
        db.add(asistencia)
    else:
        asistencia.estado = estado
        if estado != "Presente":
            asistencia.hora_llegada = None
            asistencia.hora_salida = None

    db.commit()
    return {"mensaje": f"Estado actualizado a {estado} exitosamente."}


# =====================================================================
# NUEVO: KIOSCO WEB DE EMPLEADOS (DNI) Y PORTAL RRHH / HACIENDA CON LOGIN
# =====================================================================

@app.post("/kiosco/fichar")
def fichar_por_dni(datos: FichajeKioscoRequest, db: Session = Depends(get_db)):
    dni_limpio = datos.dni.strip()
    tipo = datos.tipo.lower().strip()

    empleado = db.query(models.Empleado).filter(
        models.Empleado.dni == dni_limpio,
        models.Empleado.activo == True
    ).first()

    if not empleado:
        raise HTTPException(status_code=404, detail="No se encontró ningún empleado activo con ese DNI.")

    ahora = obtener_ahora_arg()
    hoy = ahora.date()
    hora_actual = ahora.strftime("%H:%M")

    asistencia = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id == empleado.id,
        models.Asistencia.fecha == hoy
    ).first()

    if tipo == "entrada":
        if asistencia and asistencia.estado in ["Falta", "Licencia"]:
            raise HTTPException(status_code=400, detail=f"Usted figura hoy con estado: {asistencia.estado}. Consulte en RRHH.")
        if asistencia and asistencia.hora_llegada:
            calc = calcular_tiempos_jornada(asistencia.hora_llegada, None, hoy)
            raise HTTPException(
                status_code=400,
                detail=f"Ya registraste tu entrada hoy a las {asistencia.hora_llegada} hs. Para cumplir tus 6 horas debés salir a las {calc['salida_estimada']} hs."
            )
        if not asistencia:
            asistencia = models.Asistencia(
                empleado_id=empleado.id,
                fecha=hoy,
                hora_llegada=hora_actual,
                estado="Presente",
                sincronizado=True
            )
            db.add(asistencia)
        else:
            asistencia.hora_llegada = hora_actual
            asistencia.estado = "Presente"

        db.commit()
        calc = calcular_tiempos_jornada(hora_actual, None, hoy)
        return {
            "empleado": empleado.nombre_completo,
            "sector": empleado.sector.nombre if empleado.sector else "Sin sector",
            "mensaje": f"¡Entrada registrada a las {hora_actual} hs!",
            "submensaje": f"Para completar tus 6 horas de jornada, tu horario de salida hoy es a las {calc['salida_estimada']} hs."
        }

    elif tipo == "salida":
        if not asistencia or not asistencia.hora_llegada:
            raise HTTPException(status_code=400, detail="No tenés una entrada registrada hoy. Primero debés marcar entrada.")
        if asistencia.hora_salida:
            calc = calcular_tiempos_jornada(asistencia.hora_llegada, asistencia.hora_salida, hoy)
            raise HTTPException(status_code=400, detail=f"Ya registraste tu salida hoy a las {asistencia.hora_salida} hs (Trabajado: {calc['trabajado_str']}).")

        asistencia.hora_salida = hora_actual
        db.commit()
        calc = calcular_tiempos_jornada(asistencia.hora_llegada, hora_actual, hoy)

        estado_jornada = "✅ ¡Jornada de 6 horas completada!" if calc["cumplio_jornada"] else f"⚠️ Atención: Te retiraste antes de las {calc['salida_estimada']} hs (menos de 6 hs)."
        return {
            "empleado": empleado.nombre_completo,
            "sector": empleado.sector.nombre if empleado.sector else "Sin sector",
            "mensaje": f"¡Salida registrada a las {hora_actual} hs!",
            "submensaje": f"Tiempo total trabajado hoy: {calc['trabajado_str']}. {estado_jornada}"
        }
    else:
        raise HTTPException(status_code=400, detail="Tipo de fichaje inválido.")


# --- RUTA PROTEGIDA CON LOGIN DE ADMIN PARA DESCARGAR EXCEL DE HACIENDA ---
@app.get("/rrhh/exportar-liquidacion")
def exportar_liquidacion_rrhh(
    desde: date,
    hasta: date,
    sector_id: int | None = None,
    db: Session = Depends(get_db),
    admin: models.Encargado = Depends(obtener_admin_actual)
):
    query = db.query(models.Asistencia).join(models.Empleado).filter(
        models.Asistencia.fecha >= desde,
        models.Asistencia.fecha <= hasta
    )
    if sector_id:
        query = query.filter(models.Empleado.sector_id == sector_id)

    asistencias = query.order_by(models.Empleado.nombre_completo, models.Asistencia.fecha).all()

    output = io.StringIO()
    output.write('\ufeff') # BOM para que Excel abra acentos y columnas perfecto
    writer = csv.writer(output, delimiter=';')

    writer.writerow(["--- DETALLE DIARIO DE TARJA MUNICIPAL ---"])
    writer.writerow([
        "Legajo", "DNI", "Empleado", "Sector", "Fecha", "Estado",
        "Hora Entrada", "Salida Exigida (6hs)", "Hora Salida Real",
        "Tiempo Trabajado", "Minutos Computados", "Cumplio 6hs"
    ])

    resumen = {}

    for a in asistencias:
        emp = a.empleado
        sec_nombre = emp.sector.nombre if emp.sector else "Sin sector"
        estado_real = a.estado if a.estado else "Presente"

        if estado_real == "Presente" and a.hora_llegada and a.hora_salida:
            calc = calcular_tiempos_jornada(a.hora_llegada, a.hora_salida, a.fecha)
        elif estado_real == "Presente" and a.hora_llegada:
            calc = calcular_tiempos_jornada(a.hora_llegada, None, a.fecha)
        else:
            calc = {"trabajado_str": "00h 00m", "salida_estimada": "-", "minutos_totales": 0, "cumplio_jornada": False}

        writer.writerow([
            emp.legajo, emp.dni, emp.nombre_completo, sec_nombre,
            a.fecha.strftime("%d/%m/%Y"), estado_real,
            a.hora_llegada or "-", calc["salida_estimada"], a.hora_salida or ("En curso" if a.hora_llegada and estado_real == "Presente" else "-"),
            calc["trabajado_str"], calc["minutos_totales"],
            "SI" if calc["cumplio_jornada"] else "NO"
        ])

        if emp.id not in resumen:
            resumen[emp.id] = {
                "legajo": emp.legajo, "dni": emp.dni, "nombre": emp.nombre_completo,
                "sector": sec_nombre, "presentes": 0, "faltas": 0, "licencias": 0, "minutos": 0
            }

        if estado_real == "Falta":
            resumen[emp.id]["faltas"] += 1
        elif estado_real == "Licencia":
            resumen[emp.id]["licencias"] += 1
        else:
            resumen[emp.id]["presentes"] += 1
            if a.hora_llegada and a.hora_salida:
                resumen[emp.id]["minutos"] += calc["minutos_totales"]

    writer.writerow([])
    writer.writerow(["--- RESUMEN ACUMULADO PARA LIQUIDACIÓN DE SUELDOS (HACIENDA) ---"])
    writer.writerow(["Periodo:", f"Del {desde.strftime('%d/%m/%Y')} al {hasta.strftime('%d/%m/%Y')}"])
    writer.writerow(["Legajo", "DNI", "Empleado", "Sector", "Dias Trabajados", "Faltas Injustificadas", "Licencias", "TOTAL HORAS ACUMULADAS", "Total en Decimal (Hs)"])

    for emp_id, d in resumen.items():
        h_tot = d["minutos"] // 60
        m_tot = d["minutos"] % 60
        hs_decimal = round(d["minutos"] / 60.0, 2)
        writer.writerow([
            d["legajo"], d["dni"], d["nombre"], d["sector"],
            d["presentes"], d["faltas"], d["licencias"],
            f"{h_tot} hs {m_tot} min", str(hs_decimal).replace('.', ',')
        ])

    output.seek(0)
    filename = f"Liquidacion_Muni_{desde}_al_{hasta}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# --- ANEXO WEB COMPLETO (TERMINAL DNI + PORTAL RRHH CON LOGIN) ---
@app.get("/web", response_class=HTMLResponse)
def portal_web_municipal():
    return """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Sistema de Tarja Digital Municipal</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-100 min-h-screen font-sans">
        <nav class="bg-purple-900 text-white px-6 py-4 shadow-lg flex flex-wrap justify-between items-center gap-4">
            <div class="flex items-center space-x-3">
                <span class="text-2xl">🏛️</span>
                <div>
                    <h1 class="font-bold text-lg leading-tight">Municipalidad - Tarja Digital</h1>
                    <p class="text-xs text-purple-200">Terminal de Autogestión y Portal de Liquidación</p>
                </div>
            </div>
            <div class="flex space-x-2">
                <button onclick="cambiarPestana('kiosco')" id="btn-tab-kiosco" class="bg-white text-purple-900 px-4 py-2 rounded-lg font-bold shadow">
                    🖥️ Terminal DNI (Personal)
                </button>
                <button onclick="cambiarPestana('rrhh')" id="btn-tab-rrhh" class="bg-purple-700 hover:bg-purple-600 text-white px-4 py-2 rounded-lg font-semibold">
                    🔒 RRHH / Hacienda
                </button>
            </div>
        </nav>

        <!-- SECCIÓN 1: KIOSCO DE FICHAJE POR DNI -->
        <div id="vista-kiosco" class="max-w-lg mx-auto mt-10 bg-white p-8 rounded-2xl shadow-xl border border-slate-200 text-center">
            <h2 class="text-2xl font-extrabold text-slate-800">Registro Diario de Asistencia</h2>
            <p class="text-slate-500 text-sm mt-1 mb-6">Ingrese su número de DNI sin puntos y presione Entrada o Salida</p>

            <div class="bg-slate-900 text-emerald-400 font-mono text-5xl font-bold py-4 rounded-xl mb-6 shadow-inner" id="reloj-vivo">
                00:00:00
            </div>

            <input type="number" id="dni-kiosco" placeholder="Ej: 35123456" autofocus
                class="w-full text-center text-3xl font-bold p-4 border-2 border-slate-300 rounded-xl mb-6 focus:border-purple-600 focus:outline-none">

            <div class="grid grid-cols-2 gap-4">
                <button onclick="registrarFichaje('entrada')" class="bg-emerald-600 hover:bg-emerald-700 active:scale-95 transition text-white font-bold py-5 rounded-xl text-xl shadow-lg">
                    🟢 ENTRADA
                </button>
                <button onclick="registrarFichaje('salida')" class="bg-orange-500 hover:bg-orange-600 active:scale-95 transition text-white font-bold py-5 rounded-xl text-xl shadow-lg">
                    🟠 SALIDA
                </button>
            </div>

            <div id="alerta-kiosco" class="mt-6 p-5 rounded-xl hidden text-left"></div>
        </div>

        <!-- SECCIÓN 2: PORTAL RRHH Y HACIENDA (CON LOGIN) -->
        <div id="vista-rrhh" class="max-w-4xl mx-auto mt-10 hidden">
            
            <!-- TARJETA DE LOGIN -->
            <div id="caja-login-rrhh" class="max-w-md mx-auto bg-white p-8 rounded-2xl shadow-xl border border-slate-200">
                <div class="text-center mb-6">
                    <div class="text-4xl mb-2">🔐</div>
                    <h2 class="text-2xl font-bold text-slate-800">Acceso RRHH y Hacienda</h2>
                    <p class="text-slate-500 text-sm">Ingrese credenciales de Administrador para descargar liquidaciones</p>
                </div>
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-bold text-slate-700 mb-1">Usuario Administrador</label>
                        <input type="text" id="user-rrhh" class="w-full p-3 border rounded-lg focus:outline-purple-600" placeholder="admin">
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-700 mb-1">Contraseña</label>
                        <input type="password" id="pass-rrhh" class="w-full p-3 border rounded-lg focus:outline-purple-600" placeholder="••••••••">
                    </div>
                    <div id="error-login" class="text-red-600 text-sm font-bold hidden"></div>
                    <button onclick="iniciarSesionRRHH()" class="w-full bg-purple-800 hover:bg-purple-900 text-white font-bold py-3 rounded-xl shadow">
                        INGRESAR AL PANEL
                    </button>
                </div>
            </div>

            <!-- PANEL DESBLOQUEADO DE RRHH / HACIENDA -->
            <div id="panel-descargas-rrhh" class="bg-white p-8 rounded-2xl shadow-xl border border-slate-200 hidden">
                <div class="flex justify-between items-center border-b pb-4 mb-6">
                    <div>
                        <h2 class="text-2xl font-bold text-slate-800">Panel de Reportes y Liquidación de Horas</h2>
                        <p class="text-slate-500 text-sm">Exporte las planillas en formato Excel (.CSV) listas para Recursos Humanos y Hacienda.</p>
                    </div>
                    <button onclick="cerrarSesionRRHH()" class="bg-red-100 hover:bg-red-200 text-red-700 font-bold px-4 py-2 rounded-lg text-sm">
                        Cerrar Sesión
                    </button>
                </div>

                <!-- BOTONES RÁPIDOS DE RANGO -->
                <label class="block text-sm font-bold text-slate-700 mb-2">1. Seleccione el período rápido:</label>
                <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                    <button onclick="seleccionarPeriodo('dia')" class="bg-blue-50 hover:bg-blue-100 border border-blue-300 text-blue-900 font-bold py-4 rounded-xl">
                        📅 Tarja Diaria (Hoy)
                    </button>
                    <button onclick="seleccionarPeriodo('semana')" class="bg-purple-50 hover:bg-purple-100 border border-purple-300 text-purple-900 font-bold py-4 rounded-xl">
                        📆 Tarja Semanal (Últimos 7 días)
                    </button>
                    <button onclick="seleccionarPeriodo('mes')" class="bg-emerald-50 hover:bg-emerald-100 border border-emerald-300 text-emerald-900 font-bold py-4 rounded-xl">
                        📊 Tarja Mensual (Mes Completo)
                    </button>
                </div>

                <!-- FILTROS PERSONALIZADOS -->
                <div class="grid grid-cols-1 md:grid-cols-3 gap-4 bg-slate-50 p-5 rounded-xl border mb-6">
                    <div>
                        <label class="block text-sm font-bold text-slate-700 mb-1">Fecha Desde:</label>
                        <input type="date" id="fecha-desde" class="w-full p-2.5 border rounded-lg bg-white">
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-700 mb-1">Fecha Hasta:</label>
                        <input type="date" id="fecha-hasta" class="w-full p-2.5 border rounded-lg bg-white">
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-700 mb-1">Filtrar por Sector:</label>
                        <select id="filtro-sector" class="w-full p-2.5 border rounded-lg bg-white">
                            <option value="">Todos los sectores municipales</option>
                        </select>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <button onclick="verTablaEnVivo()" class="bg-slate-800 hover:bg-slate-900 text-white font-bold py-4 rounded-xl shadow">
                        👁️ VER TARJA DE HOY EN PANTALLA
                    </button>
                    <button onclick="descargarPlanillaHacienda()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-bold py-4 rounded-xl shadow">
                        📥 DESCARGAR EXCEL PARA HACIENDA (.CSV)
                    </button>
                </div>

                <!-- TABLA DE VISTA PREVIA EN VIVO -->
                <div id="contenedor-tabla-vivo" class="mt-8 hidden">
                    <h3 class="font-bold text-lg text-slate-800 mb-3">Estado del Personal Hoy (Tiempo Real)</h3>
                    <div class="overflow-x-auto border rounded-xl">
                        <table class="w-full text-left border-collapse text-sm">
                            <thead class="bg-slate-200 text-slate-700">
                                <tr>
                                    <th class="p-3">Legajo</th>
                                    <th class="p-3">Empleado</th>
                                    <th class="p-3">Sector</th>
                                    <th class="p-3">Entrada</th>
                                    <th class="p-3">Salida Exigida (6hs)</th>
                                    <th class="p-3">Salida Real</th>
                                    <th class="p-3">Tiempo Trabajado</th>
                                </tr>
                            </thead>
                            <tbody id="tbody-vivo" class="divide-y"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>

        <script>
            let tokenAdmin = localStorage.getItem('tarja_rrhh_token') || null;

            setInterval(() => {
                const ahora = new Date();
                document.getElementById('reloj-vivo').innerText = ahora.toLocaleTimeString('es-AR', {hour12: false});
            }, 1000);

            // Permitir presionar Enter en el DNI para marcar entrada rápido
            document.getElementById('dni-kiosco').addEventListener('keypress', function (e) {
                if (e.key === 'Enter') registrarFichaje('entrada');
            });

            function cambiarPestana(tab) {
                const esKiosco = (tab === 'kiosco');
                document.getElementById('vista-kiosco').classList.toggle('hidden', !esKiosco);
                document.getElementById('vista-rrhh').classList.toggle('hidden', esKiosco);
                
                document.getElementById('btn-tab-kiosco').className = esKiosco 
                    ? "bg-white text-purple-900 px-4 py-2 rounded-lg font-bold shadow"
                    : "bg-purple-700 hover:bg-purple-600 text-white px-4 py-2 rounded-lg font-semibold";
                document.getElementById('btn-tab-rrhh').className = !esKiosco 
                    ? "bg-white text-purple-900 px-4 py-2 rounded-lg font-bold shadow"
                    : "bg-purple-700 hover:bg-purple-600 text-white px-4 py-2 rounded-lg font-semibold";

                if (!esKiosco) verificarEstadoLogin();
            }

            async function registrarFichaje(tipo) {
                const input = document.getElementById('dni-kiosco');
                const dni = input.value.trim();
                const alerta = document.getElementById('alerta-kiosco');

                if (!dni) {
                    alerta.className = "mt-6 p-4 rounded-xl bg-amber-100 text-amber-900 border border-amber-300 block font-bold text-center";
                    alerta.innerText = "⚠️ Por favor ingrese su número de DNI.";
                    return;
                }

                alerta.className = "mt-6 p-4 rounded-xl bg-slate-100 text-slate-700 block text-center font-semibold";
                alerta.innerText = "Procesando registro...";

                try {
                    const res = await fetch('/kiosco/fichar', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({dni: dni, tipo: tipo})
                    });
                    const data = await res.json();

                    if (res.ok) {
                        alerta.className = "mt-6 p-5 rounded-xl bg-emerald-100 text-emerald-950 border-2 border-emerald-400 block";
                        alerta.innerHTML = `
                            <div class="text-xl font-extrabold">${data.empleado}</div>
                            <div class="text-xs uppercase tracking-wider text-emerald-700 font-bold mb-2">Sector: ${data.sector}</div>
                            <div class="font-bold text-lg">${data.mensaje}</div>
                            <div class="mt-1 text-sm bg-white/70 p-2 rounded border border-emerald-300 font-semibold">${data.submensaje}</div>
                        `;
                        input.value = "";
                        input.focus();
                    } else {
                        alerta.className = "mt-6 p-5 rounded-xl bg-red-100 text-red-900 border-2 border-red-300 block font-bold";
                        alerta.innerText = "❌ " + (data.detail || "No se pudo registrar.");
                    }
                } catch (err) {
                    alerta.className = "mt-6 p-5 rounded-xl bg-red-100 text-red-900 block font-bold";
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
                        if (data.rol !== 'admin') {
                            errBox.innerText = "Acceso denegado: Solo cuentas de Administrador pueden ingresar a RRHH.";
                            errBox.classList.remove('hidden');
                            return;
                        }
                        tokenAdmin = data.access_token;
                        localStorage.setItem('tarja_rrhh_token', tokenAdmin);
                        verificarEstadoLogin();
                    } else {
                        errBox.innerText = data.detail || "Usuario o contraseña incorrectos.";
                        errBox.classList.remove('hidden');
                    }
                } catch (e) {
                    errBox.innerText = "Error al conectar con el servidor.";
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

                // Comprobar si el token sigue siendo válido cargando sectores
                const res = await fetch('/admin/sectores', {
                    headers: {'Authorization': 'Bearer ' + tokenAdmin}
                });

                if (res.ok) {
                    const sectores = await res.json();
                    const select = document.getElementById('filtro-sector');
                    select.innerHTML = '<option value="">Todos los sectores municipales</option>';
                    sectores.forEach(s => {
                        select.innerHTML += `<option value="${s.id}">${s.nombre}</option>`;
                    });
                    seleccionarPeriodo('dia');
                    loginBox.classList.add('hidden');
                    panelBox.classList.remove('hidden');
                } else {
                    cerrarSesionRRHH();
                }
            }

            function seleccionarPeriodo(tipo) {
                const hoy = new Date();
                let desde = new Date();
                if (tipo === 'semana') desde.setDate(hoy.getDate() - 7);
                if (tipo === 'mes') desde = new Date(hoy.getFullYear(), hoy.getMonth(), 1);

                document.getElementById('fecha-desde').value = desde.toISOString().split('T')[0];
                document.getElementById('fecha-hasta').value = hoy.toISOString().split('T')[0];
            }

            async function descargarPlanillaHacienda() {
                const d = document.getElementById('fecha-desde').value;
                const h = document.getElementById('fecha-hasta').value;
                const sec = document.getElementById('filtro-sector').value;

                if (!d || !h) { alert("Seleccione las fechas Desde y Hasta"); return; }

                let url = `/rrhh/exportar-liquidacion?desde=${d}&hasta=${h}`;
                if (sec) url += `&sector_id=${sec}`;

                const res = await fetch(url, {
                    headers: {'Authorization': 'Bearer ' + tokenAdmin}
                });

                if (res.ok) {
                    const blob = await res.blob();
                    const link = document.createElement('a');
                    link.href = window.URL.createObjectURL(blob);
                    link.download = `Liquidacion_Muni_${d}_al_${h}.csv`;
                    link.click();
                } else {
                    alert("Error al descargar el reporte. Verifique su sesión.");
                }
            }

            async function verTablaEnVivo() {
                const hoyStr = new Date().toISOString().split('T')[0];
                const secFiltro = document.getElementById('filtro-sector').value;
                const res = await fetch(`/admin/asistencias/${hoyStr}`, {
                    headers: {'Authorization': 'Bearer ' + tokenAdmin}
                });

                if (res.ok) {
                    let datos = await res.json();
                    if (secFiltro) datos = datos.filter(d => String(d.sector_id) === String(secFiltro));
                    
                    const tbody = document.getElementById('tbody-vivo');
                    tbody.innerHTML = "";
                    datos.forEach(a => {
                        tbody.innerHTML += `
                            <tr class="hover:bg-slate-50">
                                <td class="p-3 font-mono">${a.legajo || '-'}</td>
                                <td class="p-3 font-bold">${a.nombre}</td>
                                <td class="p-3">${a.sector}</td>
                                <td class="p-3 text-emerald-700 font-semibold">${a.hora_llegada || '-'}</td>
                                <td class="p-3 text-slate-600">${a.salida_estimada || '-'}</td>
                                <td class="p-3 text-orange-600 font-semibold">${a.hora_salida || (a.en_curso ? 'En curso...' : '-')}</td>
                                <td class="p-3 font-bold ${a.en_curso ? 'text-blue-700' : 'text-emerald-800'}">${a.estado !== 'Presente' ? a.estado : a.tiempo_trabajado}</td>
                            </tr>
                        `;
                    });
                    document.getElementById('contenedor-tabla-vivo').classList.remove('hidden');
                }
            }
        </script>
    </body>
    </html>
    """