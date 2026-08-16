from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, date
from passlib.context import CryptContext
from jose import JWTError, jwt
from typing import List
import base64
import requests
from sqlalchemy import extract
from datetime import date # Asegurate de que 'date' esté importado arriba de todo

from datetime import date, datetime
from fastapi import FastAPI, Depends, HTTPException #


from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image
import os

import io
import os  # <-- Agregá esto

import models
import schemas
from database import engine, get_db
from pydantic import BaseModel

import io
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# Crea las tablas en la base de datos si no existen
models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Tarja Backend API")

# --- CONFIGURACIÓN DE SEGURIDAD (JWT) ---
SECRET_KEY = "tu_super_clave_secreta_aqui"  # En un proyecto real, esto va en variables de entorno
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # El token dura 1 semana (ideal para trabajo offline prolongado)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

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
# Esta función verifica el Token en cada petición protegida y nos devuelve quién es el encargado logueado
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


# --- RUTAS DE LA API ---

# 1. Crear Sectores
@app.post("/sectores/", response_model=schemas.Sector)
def crear_sector(sector: schemas.SectorCreate, db: Session = Depends(get_db)):
    db_sector = models.Sector(nombre=sector.nombre)
    db.add(db_sector)
    db.commit()
    db.refresh(db_sector)
    return db_sector

# 2. Registrar Encargados
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

# 3. Iniciar Sesión (Login)
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
    
    # Le enviamos el sector_id a Flutter
    return {"access_token": access_token, "token_type": "bearer", "sector_id": usuario.sector_id}

# 4. Crear Empleados
@app.post("/empleados/", response_model=schemas.Empleado)
def crear_empleado(empleado: schemas.EmpleadoCreate, db: Session = Depends(get_db)):
    nuevo_empleado = models.Empleado(
        dni=empleado.dni,
        nombre_completo=empleado.nombre_completo,
        legajo=empleado.legajo,
        sector_id=empleado.sector_id
    )
    db.add(nuevo_empleado)
    db.commit()
    db.refresh(nuevo_empleado)
    return nuevo_empleado

# ---------------------------------------------------------
# DESCARGAR EMPLEADOS DEL SECTOR (Para la App Móvil)
# ---------------------------------------------------------
@app.get("/empleados")
def obtener_empleados_sector(
    db: Session = Depends(get_db),
    usuario_actual: models.Encargado = Depends(obtener_usuario_actual)
):
    # Buscamos en la base de datos solo los empleados que tengan el mismo sector_id que el encargado
    empleados = db.query(models.Empleado).filter(
        models.Empleado.sector_id == usuario_actual.sector_id
    ).all()
    
    return empleados

# 6. Sincronizar Asistencias (Protegida - Anti Duplicados)
@app.post("/asistencias/sincronizar")
def sincronizar_asistencias(
    asistencias: List[schemas.AsistenciaCreate], 
    db: Session = Depends(get_db), 
    usuario_actual: models.Encargado = Depends(obtener_usuario_actual)
):
    for asis in asistencias:
        # Buscamos si ya existe un registro de este empleado en esta fecha exacta
        registro_existente = db.query(models.Asistencia).filter(
            models.Asistencia.empleado_id == asis.empleado_id,
            models.Asistencia.fecha == asis.fecha
        ).first()

        if registro_existente:
            # Si ya existe, actualizamos los horarios que lleguen desde el celular
            if asis.hora_llegada:
                registro_existente.hora_llegada = asis.hora_llegada
            if asis.hora_salida:
                registro_existente.hora_salida = asis.hora_salida
        else:
            # Si no existe, creamos el registro nuevo con los horarios
            nueva_asistencia = models.Asistencia(
                empleado_id=asis.empleado_id,
                fecha=asis.fecha,
                hora_llegada=asis.hora_llegada,
                hora_salida=asis.hora_salida
            )
            db.add(nueva_asistencia)
            
    db.commit()
    return {"mensaje": f"Se sincronizaron {len(asistencias)} registros de horarios con éxito."}


# 7. Descargar Asistencias de Hoy (Para recuperar memoria en el celular al reinstalar)
@app.get("/asistencias/hoy")
def obtener_asistencias_hoy(
    db: Session = Depends(get_db), 
    usuario_actual: models.Encargado = Depends(obtener_usuario_actual)
):
    from datetime import date # Lo importamos acá por las dudas
    hoy = date.today()
    
    # 1. Buscar empleados del sector de este encargado
    empleados_sector = db.query(models.Empleado).filter(models.Empleado.sector_id == usuario_actual.sector_id).all()
    ids_empleados = [emp.id for emp in empleados_sector]

    # 2. Buscar si esos empleados marcaron asistencia hoy
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

# ------------------------------------------
# OPCIÓN 1: DESCARGAR PDF DIRECTO (GET)
# ------------------------------------------
@app.get("/reporte/descargar_pdf")
def descargar_pdf(
    db: Session = Depends(get_db),
    usuario_actual: models.Encargado = Depends(obtener_usuario_actual)
):
    hoy = date.today()
    
    # 1. Buscar SOLO los empleados que pertenecen al sector del encargado
    empleados_sector = db.query(models.Empleado).filter(models.Empleado.sector_id == usuario_actual.sector_id).all()
    ids_empleados = [emp.id for emp in empleados_sector]

    if not ids_empleados:
        raise HTTPException(status_code=404, detail="No hay empleados asignados a tu sector.")

    # 2. Filtrar asistencias de hoy SOLO para esos empleados
    asistencias_hoy = db.query(models.Asistencia).filter(
        models.Asistencia.fecha == hoy,
        models.Asistencia.empleado_id.in_(ids_empleados)
    ).all()
    
    if not asistencias_hoy:
        raise HTTPException(status_code=404, detail="No hay registros hoy para tu sector. Sincronice primero.")

# 3. Armar el PDF en memoria
    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4)
    elementos = []
    estilos = getSampleStyleSheet()

    # --- NUEVO: INSERTAR LOGO ---
    ruta_logo = "logo_muni.png"  # El archivo de imagen tiene que estar en la misma carpeta que main.py
    if os.path.exists(ruta_logo):
        # Ancho (width) y alto (height) en puntos. Podés modificar estos números si lo ves muy grande o chico
        imagen_logo = Image(ruta_logo, width=100, height=100,preserveAspectRatio=True)
        elementos.append(imagen_logo)
        elementos.append(Spacer(1, 15)) # Un pequeño espacio entre el logo y el texto

    # Título del PDF
    titulo = Paragraph(f"Reporte de Asistencia (Sector {usuario_actual.sector_id}) - {hoy.strftime('%d/%m/%Y')}", estilos['Title'])
    elementos.append(titulo)
    elementos.append(Spacer(1, 20))

    datos_tabla = [["Legajo", "Nombre", "Llegada", "Salida"]]
    
    # Un diccionario rápido para emparejar ID con Nombre
    empleados_dict = {emp.id: emp for emp in empleados_sector}
    
    for asis in asistencias_hoy:
        empleado = empleados_dict.get(asis.empleado_id)
        if empleado:
            llegada = asis.hora_llegada if asis.hora_llegada else "No marcó"
            salida = asis.hora_salida if asis.hora_salida else "No marcó"
            datos_tabla.append([empleado.legajo, empleado.nombre_completo, llegada, salida])

    tabla = Table(datos_tabla, colWidths=[80, 200, 80, 80])
    estilo_tabla = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#ecf0f1')),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ])
    tabla.setStyle(estilo_tabla)
    elementos.append(tabla)

    pdf.build(elementos)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    nombre_archivo = f"Asistencia_Sector_{usuario_actual.sector_id}_{hoy.strftime('%Y%m%d')}.pdf"
    
    return Response(
        content=pdf_bytes, 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"}
    )

# ------------------------------------------
# OPCIÓN 2: ENVÍO POR API (POST - VÍA RESEND)
# ------------------------------------------
API_KEY_RESEND = os.environ.get("RESEND_API_KEY")  # <-- ASEGURATE DE PEGAR TU CLAVE ACÁ

@app.post("/reporte/enviar_correo_api")
def enviar_correo_api(
    request: CorreoRequest,
    db: Session = Depends(get_db),
    usuario_actual: models.Encargado = Depends(obtener_usuario_actual)
):
    hoy = date.today()
    
    # 1. Filtro estricto de empleados por sector
    empleados_sector = db.query(models.Empleado).filter(models.Empleado.sector_id == usuario_actual.sector_id).all()
    ids_empleados = [emp.id for emp in empleados_sector]

    if not ids_empleados:
        raise HTTPException(status_code=404, detail="No hay empleados en tu sector.")

    # 2. Asistencias correspondientes al sector
    asistencias_hoy = db.query(models.Asistencia).filter(
        models.Asistencia.fecha == hoy,
        models.Asistencia.empleado_id.in_(ids_empleados)
    ).all()
    
    if not asistencias_hoy:
        raise HTTPException(status_code=404, detail="No hay registros hoy para tu sector. Sincronice primero.")

    # 3. Armar PDF
    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4)
    elementos = []
    estilos = getSampleStyleSheet()

    titulo = Paragraph(f"Reporte de Asistencia (Sector {usuario_actual.sector_id}) - {hoy.strftime('%d/%m/%Y')}", estilos['Title'])
    elementos.append(titulo)
    elementos.append(Spacer(1, 20))

    datos_tabla = [["Legajo", "Nombre", "Llegada", "Salida"]]
    empleados_dict = {emp.id: emp for emp in empleados_sector}
    
    for asis in asistencias_hoy:
        empleado = empleados_dict.get(asis.empleado_id)
        if empleado:
            llegada = asis.hora_llegada if asis.hora_llegada else "No marcó"
            salida = asis.hora_salida if asis.hora_salida else "No marcó"
            datos_tabla.append([empleado.legajo, empleado.nombre_completo, llegada, salida])

    tabla = Table(datos_tabla, colWidths=[80, 200, 80, 80])
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

    # 4. Enviar a través de Resend
    pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')

    url_resend = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {API_KEY_RESEND}",
        "Content-Type": "application/json"
    }
    payload = {
        "from": "Tarja Obras <onboarding@resend.dev>",
        "to": [request.correo_destino],
        "subject": f"Cierre de Jornada Tarja - Sector {usuario_actual.sector_id} - {hoy.strftime('%d/%m/%Y')}",
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



# ------------------------------------------
# REPORTE DE HORAS POR RANGO DE FECHAS (PDF)
# ------------------------------------------
@app.get("/reporte/rango_pdf")
def reporte_rango_pdf(
    fecha_inicio: date,
    fecha_fin: date,
    db: Session = Depends(get_db),
    usuario_actual: models.Encargado = Depends(obtener_usuario_actual)
):
    # 1. NUEVO: Buscar el nombre real del sector en la base de datos
    sector_info = db.query(models.Sector).filter(models.Sector.id == usuario_actual.sector_id).first()
    nombre_sector = sector_info.nombre if sector_info else f"Sector {usuario_actual.sector_id}"

    # 2. Buscar empleados del sector
    empleados_sector = db.query(models.Empleado).filter(models.Empleado.sector_id == usuario_actual.sector_id).all()
    ids_empleados = [emp.id for emp in empleados_sector]

    if not ids_empleados:
        raise HTTPException(status_code=404, detail="No hay empleados asignados.")

    # 3. Filtrar asistencias por RANGO de fechas
    asistencias_rango = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id.in_(ids_empleados),
        models.Asistencia.fecha >= fecha_inicio,
        models.Asistencia.fecha <= fecha_fin
    ).all()

    if not asistencias_rango:
        raise HTTPException(status_code=404, detail=f"No hay registros entre el {fecha_inicio.strftime('%d/%m/%Y')} y el {fecha_fin.strftime('%d/%m/%Y')}.")

    # 4. Matemática: Calcular minutos trabajados por empleado
    minutos_por_empleado = {emp.id: 0 for emp in empleados_sector}
    
    for asis in asistencias_rango:
        if asis.hora_llegada and asis.hora_salida:
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

    # 5. Armar el PDF
    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4)
    elementos = []
    estilos = getSampleStyleSheet()

    # Logo municipal
    ruta_logo = "logo_muni.png"
    if os.path.exists(ruta_logo):
        imagen_logo = Image(ruta_logo, width=100, height=100)
        elementos.append(imagen_logo)
        elementos.append(Spacer(1, 15))

    # Título dinámico AHORA CON EL NOMBRE DEL SECTOR
    texto_inicio = fecha_inicio.strftime('%d/%m/%Y')
    texto_fin = fecha_fin.strftime('%d/%m/%Y')
    
    titulo = Paragraph(f"Total de Horas Trabajadas<br/>Sector: {nombre_sector}<br/>(Del {texto_inicio} al {texto_fin})", estilos['Title'])
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

    # NUEVO: El archivo PDF que se descarga ahora se llama con el nombre del sector (reemplazando espacios con guiones bajos)
    nombre_archivo_limpio = nombre_sector.replace(" ", "_")
    nombre_archivo = f"Reporte_{nombre_archivo_limpio}_{texto_inicio.replace('/','-')}_al_{texto_fin.replace('/','-')}.pdf"
    
    return Response(
        content=pdf_bytes, 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"}
    )

# --- Función matemática para restar la salida menos la llegada ---
def calcular_diferencia_horas(llegada: str, salida: str) -> float:
    if not llegada or not salida:
        return 0.0
    try:
        formato = "%H:%M"
        t_llegada = datetime.strptime(llegada, formato)
        t_salida = datetime.strptime(salida, formato)
        diferencia = t_salida - t_llegada
        horas = diferencia.total_seconds() / 3600.0
        return round(horas, 2)
    except Exception:
        return 0.0

# ---------------------------------------------------------
# HISTORIAL Y TOTAL DE HORAS DE UN EMPLEADO
# ---------------------------------------------------------
@app.get("/empleados/{empleado_id}/historial")
def obtener_historial_empleado(
    empleado_id: int,
    fecha_inicio: date,
    fecha_fin: date,
    db: Session = Depends(get_db),
    usuario_actual: models.Encargado = Depends(obtener_usuario_actual)
):
    # 1. Buscamos al empleado
    empleado = db.query(models.Empleado).filter(
        models.Empleado.id == empleado_id,
        models.Empleado.sector_id == usuario_actual.sector_id
    ).first()
    
    if not empleado:
        raise HTTPException(status_code=404, detail="Empleado no encontrado")

    # 2. Buscamos todas sus asistencias en ese rango de fechas
    asistencias = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id == empleado_id,
        models.Asistencia.fecha >= fecha_inicio,
        models.Asistencia.fecha <= fecha_fin
    ).order_by(models.Asistencia.fecha).all()

    total_horas = 0.0
    detalle = []

    # 3. Sumamos las horas día por día
    for asis in asistencias:
        horas_dia = calcular_diferencia_horas(asis.hora_llegada, asis.hora_salida)
        total_horas += horas_dia
        detalle.append({
            "fecha": asis.fecha,
            "llegada": asis.hora_llegada,
            "salida": asis.hora_salida,
            "horas_trabajadas": horas_dia
        })

    return {
        "empleado": empleado.nombre_completo,
        "legajo": empleado.legajo,
        "total_horas": round(total_horas, 2), # Horas totales del mes o quincena
        "detalle": detalle
    }
