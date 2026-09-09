from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import extract
from datetime import date, datetime, timedelta, timezone
from passlib.context import CryptContext
from jose import JWTError, jwt
from typing import List
from sqlalchemy import func
from datetime import date

import base64
import requests
import os
import io

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
    
    # ¡NUEVO!: Le enviamos el rol a Flutter para que decida qué pantalla abrir
    return {
        "access_token": access_token, 
        "token_type": "bearer", 
        "sector_id": usuario.sector_id,
        "rol": usuario.rol
    }

# ==========================================
# ESQUEMAS (Para recibir datos en las rutas)
# ==========================================
class SectorNuevo(BaseModel):
    nombre: str

class EmpleadoNuevo(BaseModel):
    dni: str
    nombre_completo: str
    legajo: str
    sector_id: int

# ==========================================
# CANDADO DE SEGURIDAD PARA EL ADMINISTRADOR
# ==========================================
def obtener_admin_actual(usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    """Verifica que el usuario logueado tenga el rol de 'admin'."""
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
def ver_sectores_admin(
    db: Session = Depends(get_db),
    admin: models.Encargado = Depends(obtener_admin_actual)
):
    return db.query(models.Sector).all()

@app.get("/admin/empleados")
def ver_todos_los_empleados_admin(
    db: Session = Depends(get_db),
    admin: models.Encargado = Depends(obtener_admin_actual)
):
    # Buscamos todos los empleados activos
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
def editar_empleado_admin(
    empleado_id: int, 
    datos: EmpleadoNuevo, 
    db: Session = Depends(get_db), 
    admin: models.Encargado = Depends(obtener_admin_actual)
):
    empleado = db.query(models.Empleado).filter(models.Empleado.id == empleado_id).first()
    if not empleado:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    
    # Verificar que el DNI o legajo nuevo no le pertenezcan a OTRO empleado distinto
    duplicado = db.query(models.Empleado).filter(
        models.Empleado.id != empleado_id,
        (models.Empleado.dni == datos.dni) | (models.Empleado.legajo == datos.legajo)
    ).first()
    
    if duplicado:
        raise HTTPException(status_code=400, detail="El DNI o Legajo ya pertenece a otro empleado.")
    
    # Actualizamos los datos
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

@app.get("/admin/asistencias/{fecha_solicitada}")
def obtener_todas_asistencias(fecha_solicitada: date, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    asistencias = db.query(models.Asistencia).filter(models.Asistencia.fecha == fecha_solicitada).all()
    
    resultado = []
    for asis in asistencias:
        resultado.append({
            "empleado_id": asis.empleado_id,
            "nombre": asis.empleado.nombre_completo,
            "legajo": asis.empleado.legajo, # <-- NUEVO
            "sector_id": asis.empleado.sector_id, # <-- NUEVO
            "sector": asis.empleado.sector.nombre if asis.empleado.sector else "Sin sector",
            "hora_llegada": asis.hora_llegada,
            "hora_salida": asis.hora_salida,
            "estado": asis.estado
        })
    return resultado

# Esquema para recibir los datos del nuevo encargado
class EncargadoNuevoAdmin(BaseModel):
    usuario: str
    password: str
    sector_id: int

@app.get("/admin/encargados")
def ver_encargados_admin(
    db: Session = Depends(get_db),
    admin: models.Encargado = Depends(obtener_admin_actual)
):
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
def crear_encargado_admin(
    datos: EncargadoNuevoAdmin,
    db: Session = Depends(get_db),
    admin: models.Encargado = Depends(obtener_admin_actual)
):
    existe = db.query(models.Encargado).filter(models.Encargado.usuario == datos.usuario).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ese nombre de usuario ya existe.")
    
    nuevo_encargado = models.Encargado(
        usuario=datos.usuario,
        password_hash=get_password_hash(datos.password), # Encriptamos la clave
        sector_id=datos.sector_id,
        rol="encargado"
    )
    db.add(nuevo_encargado)
    db.commit()
    return {"mensaje": f"Usuario {datos.usuario} creado con éxito."}

@app.get("/admin/dashboard")
def obtener_dashboard_admin(db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    hoy = date.today()
    
    # 1. ESTADÍSTICAS DEL DÍA DE HOY
    total_empleados = db.query(models.Empleado).count()
    
    # Agrupamos las asistencias de hoy por "estado" y las contamos
    asistencias_hoy = db.query(
        models.Asistencia.estado, 
        func.count(models.Asistencia.id)
    ).filter(models.Asistencia.fecha == hoy).group_by(models.Asistencia.estado).all()
    
    presentes = 0
    faltas = 0
    licencias = 0
    
    for estado, cantidad in asistencias_hoy:
        if estado == "Presente":
            presentes = cantidad
        elif estado == "Falta":
            faltas = cantidad
        elif estado == "Licencia":
            licencias = cantidad
            
    # Los que todavía no marcaron ni llegada ni falta
    sin_marcar = total_empleados - (presentes + faltas + licencias)
    if sin_marcar < 0: sin_marcar = 0
    
    # 2. RANKING DE FALTAS DEL MES (Top 3 sectores)
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
# DESCARGAR EMPLEADOS DEL SECTOR (Para la App Móvil)
# ---------------------------------------------------------
@app.get("/empleados")
def obtener_empleados_sector(db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    empleados = db.query(models.Empleado).filter(
        models.Empleado.sector_id == usuario_actual.sector_id,
        models.Empleado.activo == True
    ).all()
    return empleados

# 6. Sincronizar Asistencias
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
                hora_salida=asis.hora_salida
            )
            db.add(nueva_asistencia)
            
    db.commit()
    return {"mensaje": f"Se sincronizaron {len(asistencias)} registros con éxito."}

# 7. Descargar Asistencias de Hoy
@app.get("/asistencias/hoy")
def obtener_asistencias_hoy(db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    zona_argentina = timezone(timedelta(hours=-3))
    hoy = datetime.now(zona_argentina).date()
    
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

# ------------------------------------------
# OPCIÓN 1: DESCARGAR PDF DIRECTO (GET)
# ------------------------------------------
@app.get("/reporte/descargar_pdf")
def descargar_pdf(db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    zona_argentina = timezone(timedelta(hours=-3))
    hoy = datetime.now(zona_argentina).date()

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

    datos_tabla = [["Legajo", "Nombre", "Llegada", "Salida"]]
    empleados_dict = {emp.id: emp for emp in empleados_sector}
    
    for asis in asistencias_hoy:
        empleado = empleados_dict.get(asis.empleado_id)
        if empleado:
            # Si el estado no es "Presente", mostramos la falta/licencia
            if asis.estado != "Presente":
                llegada = asis.estado
                salida = asis.estado
            else:
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

    nombre_archivo = f"Asistencia_{nombre_sector.replace(' ', '_')}_{hoy.strftime('%Y%m%d')}.pdf"
    
    return Response(
        content=pdf_bytes, 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"}
    )

# ------------------------------------------
# OPCIÓN 2: ENVÍO POR API (POST - VÍA RESEND)
# ------------------------------------------
API_KEY_RESEND = os.environ.get("RESEND_API_KEY") 

@app.post("/reporte/enviar_correo_api")
def enviar_correo_api(request: CorreoRequest, db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    zona_argentina = timezone(timedelta(hours=-3))
    hoy = datetime.now(zona_argentina).date()
    
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

    datos_tabla = [["Legajo", "Nombre", "Llegada", "Salida"]]
    empleados_dict = {emp.id: emp for emp in empleados_sector}
    
    for asis in asistencias_hoy:
        empleado = empleados_dict.get(asis.empleado_id)
        if empleado:
            if asis.estado != "Presente":
                llegada = asis.estado
                salida = asis.estado
            else:
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
def reporte_rango_pdf(fecha_inicio: date, fecha_fin: date, db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    zona_argentina = timezone(timedelta(hours=-3))
    ahora_arg = datetime.now(zona_argentina)
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
        if asis.hora_llegada and asis.hora_salida and asis.estado == "Presente":
            try:
                formato = "%H:%M"
                llegada = datetime.strptime(asis.hora_llegada, formato)
                salida = datetime.strptime(asis.hora_salida, formato)
                
                # Tolerancia: Si llega entre 7:01 y 7:05, redondeamos a 7:00
                if llegada.hour == 7 and 1 <= llegada.minute <= 5:
                    llegada = llegada.replace(minute=0)
                
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
        if asis.estado == "Presente":
            horas_dia = calcular_diferencia_horas(asis.hora_llegada, asis.hora_salida)
            total_horas += horas_dia
            detalle.append({
                "fecha": asis.fecha,
                "llegada": asis.hora_llegada,
                "salida": asis.hora_salida,
                "horas_trabajadas": horas_dia,
                "estado": asis.estado
            })
        else:
            detalle.append({
                "fecha": asis.fecha,
                "llegada": "-",
                "salida": "-",
                "horas_trabajadas": 0.0,
                "estado": asis.estado
            })

    return {
        "empleado": empleado.nombre_completo,
        "legajo": empleado.legajo,
        "total_horas": round(total_horas, 2),
        "detalle": detalle
    }

# ==========================================
# REGISTRAR FALTAS O LICENCIAS
# ==========================================
@app.put("/asistencias/{empleado_id}/estado")
def registrar_estado_especial(empleado_id: int, estado: str, db: Session = Depends(get_db), usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    zona_argentina = timezone(timedelta(hours=-3))
    hoy = datetime.now(zona_argentina).date()

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