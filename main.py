from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, date
from passlib.context import CryptContext
from jose import JWTError, jwt
from typing import List

import models
import schemas
from database import engine, get_db
from pydantic import BaseModel

import smtplib
from email.message import EmailMessage
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
    
    # ACÁ ESTÁ LA MAGIA: Ahora le enviamos el sector_id a Flutter
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

# 5. Descargar Empleados (Protegida)
@app.get("/empleados/mis_empleados")
def obtener_mis_empleados(db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    # Trae exclusivamente los empleados que pertenecen al sector del encargado logueado
    empleados = db.query(models.Empleado).filter(models.Empleado.sector_id == usuario_actual.sector_id).all()
    return empleados

# 6. Sincronizar Asistencias (Protegida)
@app.post("/asistencias/sincronizar")
def sincronizar_asistencias(
    asistencias: List[schemas.AsistenciaCreate], 
    db: Session = Depends(get_db), 
    usuario_actual: models.Encargado = Depends(obtener_usuario_actual)
):
    for asis in asistencias:
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

# ==========================================
#        REPORTES Y CIERRE DE JORNADA
# ==========================================
class CorreoRequest(BaseModel):
    correo_destino: str

# Configuración de tu cuenta remitente
REMITENTE_EMAIL = "svfdfacilitador@gmail.com"  # Tu Gmail
REMITENTE_PASSWORD = "dcfggsvsbtrsmxpy"  # Las 16 letras (sin espacios)

# 7. Generar PDF y Cerrar Jornada (Protegida)
@app.post("/reporte/cerrar_jornada")
def cerrar_jornada(
    request: CorreoRequest,
    db: Session = Depends(get_db),
    usuario_actual: models.Encargado = Depends(obtener_usuario_actual)
):
    hoy = date.today()
    
    # Buscamos todas las asistencias del día de hoy
    asistencias_hoy = db.query(models.Asistencia).filter(models.Asistencia.fecha == hoy).all()
    
    if not asistencias_hoy:
        raise HTTPException(status_code=404, detail="No hay registros para hoy. Sincronice primero.")

    # 1. Crear el lienzo del PDF en la memoria (sin guardarlo en el disco duro)
    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4)
    elementos = []
    estilos = getSampleStyleSheet()

    # Título del PDF
    titulo = Paragraph(f"Reporte de Asistencia - Tarja - {hoy.strftime('%d/%m/%Y')}", estilos['Title'])
    elementos.append(titulo)
    elementos.append(Spacer(1, 20))

    # 2. Armar la tabla cruzando Asistencias con la tabla de Empleados
    datos_tabla = [["Legajo", "Nombre", "Llegada", "Salida"]]
    
    for asis in asistencias_hoy:
        empleado = db.query(models.Empleado).filter(models.Empleado.id == asis.empleado_id).first()
        if empleado:
            llegada = asis.hora_llegada if asis.hora_llegada else "No marcó"
            salida = asis.hora_salida if asis.hora_salida else "No marcó"
            datos_tabla.append([empleado.legajo, empleado.nombre_completo, llegada, salida])

    # Darle diseño lindo a la tabla
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

    # 3. Construir y cerrar el archivo
    pdf.build(elementos)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    # 4. Empaquetar y enviar el correo electrónico
    try:
        msg = EmailMessage()
        msg['Subject'] = f"Cierre de Jornada Tarja - {hoy.strftime('%d/%m/%Y')}"
        msg['From'] = REMITENTE_EMAIL
        msg['To'] = request.correo_destino
        msg.set_content("Adjunto se encuentra el reporte de asistencias del turno correspondiente al día de la fecha.")
        
        # Adjuntamos el PDF que tenemos en memoria
        nombre_archivo = f"Asistencia_{hoy.strftime('%Y%m%d')}.pdf"
        msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=nombre_archivo)

        # Nos conectamos a Google con TLS y un límite de tiempo de 15 segundos
        with smtplib.SMTP('smtp.gmail.com', 587, timeout=15) as smtp:
            smtp.starttls()  # Clave para que Google acepte la conexión
            smtp.login(REMITENTE_EMAIL, REMITENTE_PASSWORD)
            smtp.send_message(msg)

        return {"mensaje": f"Reporte generado y enviado con éxito a {request.correo_destino}"}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al enviar el correo: {str(e)}")
    