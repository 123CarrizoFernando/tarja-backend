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

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="Tarja Backend API")

# --- CONFIGURACIÓN DE SEGURIDAD (JWT) ---
SECRET_KEY = "tu_super_clave_secreta_aqui"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7

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

# --- CANDADO DE SEGURIDAD PARA ADMIN Y RRHH ---
def obtener_admin_actual(usuario_actual: models.Encargado = Depends(obtener_usuario_actual)):
    """Permite el acceso tanto al rol 'admin' como al rol 'rrhh'."""
    if usuario_actual.rol not in ["admin", "rrhh"]:
        raise HTTPException(
            status_code=403, 
            detail="Acceso denegado: Se requieren permisos de Administrador o RRHH."
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

class EncargadoNuevoAdmin(BaseModel):
    usuario: str
    password: str
    sector_id: int | None = None
    rol: str = "encargado" # Puede ser "encargado", "rrhh" o "admin"

class FichajeKioscoRequest(BaseModel):
    dni: str
    tipo: str # "entrada", "salida" o "consulta"

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
# RUTAS DE ADMINISTRACIÓN Y RRHH
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

@app.post("/admin/empleados")
def crear_empleado_admin(empleado: EmpleadoNuevo, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    existe = db.query(models.Empleado).filter(
        (models.Empleado.dni == empleado.dni.strip()) | (models.Empleado.legajo == empleado.legajo.strip())
    ).first()
    if existe:
        raise HTTPException(status_code=400, detail="El DNI o Legajo ya está registrado en la base de datos.")

    nuevo_empleado = models.Empleado(
        dni=empleado.dni.strip(),
        nombre_completo=empleado.nombre_completo.strip(),
        legajo=empleado.legajo.strip(),
        sector_id=empleado.sector_id,
        activo=True
    )
    db.add(nuevo_empleado)
    db.commit()
    return {"mensaje": f"Empleado {empleado.nombre_completo} (DNI: {empleado.dni}) registrado con éxito"}

@app.put("/admin/empleados/{empleado_id}")
def editar_empleado_admin(empleado_id: int, datos: EmpleadoNuevo, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    empleado = db.query(models.Empleado).filter(models.Empleado.id == empleado_id).first()
    if not empleado:
        raise HTTPException(status_code=404, detail="Empleado no encontrado.")
    
    duplicado = db.query(models.Empleado).filter(
        models.Empleado.id != empleado_id,
        (models.Empleado.dni == datos.dni.strip()) | (models.Empleado.legajo == datos.legajo.strip())
    ).first()
    if duplicado:
        raise HTTPException(status_code=400, detail="El DNI o Legajo ya pertenece a otro empleado.")
    
    empleado.nombre_completo = datos.nombre_completo.strip()
    empleado.dni = datos.dni.strip()
    empleado.legajo = datos.legajo.strip()
    empleado.sector_id = datos.sector_id
    db.commit()
    return {"mensaje": "Datos y DNI actualizados correctamente."}

@app.get("/admin/asistencias/{fecha_solicitada}")
def obtener_todas_asistencias(fecha_solicitada: date, sector_id: int | None = None, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    query = db.query(models.Asistencia).join(models.Empleado).filter(models.Asistencia.fecha == fecha_solicitada)
    if sector_id:
        query = query.filter(models.Empleado.sector_id == sector_id)
    asistencias = query.all()
    
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
    
    rol_elegido = datos.rol if datos.rol in ["admin", "rrhh", "encargado"] else "encargado"
    nuevo_encargado = models.Encargado(
        usuario=datos.usuario.strip(),
        password_hash=get_password_hash(datos.password),
        sector_id=datos.sector_id if rol_elegido == "encargado" else None,
        rol=rol_elegido
    )
    db.add(nuevo_encargado)
    db.commit()
    return {"mensaje": f"Usuario {datos.usuario} ({rol_elegido}) creado con éxito."}

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

# ==========================================
# DESCARGA DE PDF EXCLUSIVA ADMIN / RRHH (CON FILTRO DE SECTOR)
# ==========================================
@app.get("/admin/reporte_pdf")
def descargar_pdf_admin(fecha: date | None = None, sector_id: int | None = None, db: Session = Depends(get_db), admin: models.Encargado = Depends(obtener_admin_actual)):
    fecha_consulta = fecha if fecha else obtener_ahora_arg().date()
    
    query = db.query(models.Asistencia).join(models.Empleado).filter(models.Asistencia.fecha == fecha_consulta)
    nombre_sector = "Todos los Sectores"
    
    if sector_id:
        query = query.filter(models.Empleado.sector_id == sector_id)
        sec_obj = db.query(models.Sector).filter(models.Sector.id == sector_id).first()
        if sec_obj:
            nombre_sector = sec_obj.nombre

    asistencias = query.order_by(models.Empleado.nombre_completo).all()
    if not asistencias:
        raise HTTPException(status_code=404, detail="No hay asistencias registradas para ese filtro.")

    buffer = io.BytesIO()
    pdf = SimpleDocTemplate(buffer, pagesize=A4)
    elementos = []
    estilos = getSampleStyleSheet()

    if os.path.exists("logo_muni.png"):
        elementos.append(Image("logo_muni.png", width=500, height=130))
        elementos.append(Spacer(1, 15))

    titulo = Paragraph(f"Reporte de Tarja ({nombre_sector}) - {fecha_consulta.strftime('%d/%m/%Y')}", estilos['Title'])
    elementos.append(titulo)
    elementos.append(Spacer(1, 20))

    datos_tabla = [["Legajo", "DNI", "Empleado", "Sector", "Entrada", "Salida", "Horas"]]
    for asis in asistencias:
        emp = asis.empleado
        sec_nom = emp.sector.nombre if emp.sector else "-"
        if asis.estado and asis.estado != "Presente":
            llegada, salida, trabajado = asis.estado, asis.estado, "00h 00m"
        else:
            llegada = asis.hora_llegada or "-"
            salida = asis.hora_salida or "En curso"
            calc = calcular_tiempos_jornada(asis.hora_llegada, asis.hora_salida, fecha_consulta)
            trabajado = calc["trabajado_str"]
        datos_tabla.append([emp.legajo, emp.dni, emp.nombre_completo, sec_nom, llegada, salida, trabajado])

    tabla = Table(datos_tabla, colWidths=[50, 65, 145, 85, 55, 55, 65])
    tabla.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#ecf0f1')),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    elementos.append(tabla)
    pdf.build(elementos)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    nombre_archivo = f"Tarja_{nombre_sector.replace(' ', '_')}_{fecha_consulta}.pdf"
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
        "legajo": empleado.legajo,
        "total_horas": f"{h_tot}h {m_tot}m",
        "detalle": detalle
    }

# =====================================================================
# KIOSCO PÚBLICO POR DNI (EMPLEADOS) Y CONSULTA DE HORAS EN VIVO
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
        raise HTTPException(status_code=404, detail="No se encontró ningún empleado registrado con ese DNI.")

    ahora = obtener_ahora_arg()
    hoy = ahora.date()
    hora_actual = ahora.strftime("%H:%M")

    asistencia = db.query(models.Asistencia).filter(
        models.Asistencia.empleado_id == empleado.id,
        models.Asistencia.fecha == hoy
    ).first()

    # MODO CONSULTA: El empleado ingresa con su DNI para ver cuántas horas va trabajando
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
                "legajo": empleado.legajo,
                "sector": empleado.sector.nombre if empleado.sector else "Sin sector",
                "mensaje": "Hoy todavía no registraste horario de entrada.",
                "submensaje": f"Acumulado trabajado en este mes: {acumulado_mes_str}",
                "trabajado_hoy": "00h 00m",
                "salida_estimada": "--:--",
                "acumulado_mes": acumulado_mes_str
            }
        
        calc_hoy = calcular_tiempos_jornada(asistencia.hora_llegada, asistencia.hora_salida, hoy)
        estado_txt = "En curso (Trabajando)" if calc_hoy["en_curso"] else f"Finalizado (Salió {asistencia.hora_salida})"
        return {
            "empleado": empleado.nombre_completo,
            "dni": empleado.dni,
            "legajo": empleado.legajo,
            "sector": empleado.sector.nombre if empleado.sector else "Sin sector",
            "mensaje": f"⏱️ Hoy llevás trabajado: {calc_hoy['trabajado_str']} ({estado_txt})",
            "submensaje": f"Entrada hoy: {asistencia.hora_llegada} hs | Salida p/ cumplir 6hs: {calc_hoy['salida_estimada']} hs | Total del Mes: {acumulado_mes_str}",
            "trabajado_hoy": calc_hoy["trabajado_str"],
            "salida_estimada": calc_hoy["salida_estimada"],
            "acumulado_mes": acumulado_mes_str
        }

    if tipo == "entrada":
        if asistencia and asistencia.estado in ["Falta", "Licencia"]:
            raise HTTPException(status_code=400, detail=f"Usted figura hoy con estado: {asistencia.estado}. Consulte en RRHH.")
        if asistencia and asistencia.hora_llegada:
            calc = calcular_tiempos_jornada(asistencia.hora_llegada, None, hoy)
            raise HTTPException(
                status_code=400,
                detail=f"Ya registraste tu entrada a las {asistencia.hora_llegada} hs (Llevás {calc['trabajado_str']}). Debés salir a las {calc['salida_estimada']} hs."
            )
        if not asistencia:
            asistencia = models.Asistencia(
                empleado_id=empleado.id, fecha=hoy, hora_llegada=hora_actual, estado="Presente", sincronizado=True
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
            "submensaje": f"Para completar tus 6 horas de trabajo, tu horario de salida hoy es a las {calc['salida_estimada']} hs."
        }

    elif tipo == "salida":
        if not asistencia or not asistencia.hora_llegada:
            raise HTTPException(status_code=400, detail="No tenés una entrada registrada hoy. Primero debés marcar entrada.")
        if asistencia.hora_salida:
            calc = calcular_tiempos_jornada(asistencia.hora_llegada, asistencia.hora_salida, hoy)
            raise HTTPException(status_code=400, detail=f"Ya registraste tu salida hoy a las {asistencia.hora_salida} hs (Total: {calc['trabajado_str']}).")

        asistencia.hora_salida = hora_actual
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
# EXPORTACIÓN EXCEL PARA RRHH Y HACIENDA (CON FILTRO DE SECTOR)
# =====================================================================
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
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')

    writer.writerow(["--- DETALLE DE ASISTENCIA Y HORAS TRABAJADAS ---"])
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

        if estado_real == "Presente" and a.hora_llegada:
            calc = calcular_tiempos_jornada(a.hora_llegada, a.hora_salida, a.fecha)
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

        if estado_real == "Falta": resumen[emp.id]["faltas"] += 1
        elif estado_real == "Licencia": resumen[emp.id]["licencias"] += 1
        else:
            resumen[emp.id]["presentes"] += 1
            resumen[emp.id]["minutos"] += calc["minutos_totales"]

    writer.writerow([])
    writer.writerow(["--- RESUMEN ACUMULADO PARA LIQUIDACIÓN (HACIENDA) ---"])
    writer.writerow(["Legajo", "DNI", "Empleado", "Sector", "Dias Presente", "Faltas", "Licencias", "TOTAL HORAS TRABAJADAS", "Horas en Decimal"])

    for emp_id, d in resumen.items():
        h_tot = d["minutos"] // 60
        m_tot = d["minutos"] % 60
        hs_dec = round(d["minutos"] / 60.0, 2)
        writer.writerow([
            d["legajo"], d["dni"], d["nombre"], d["sector"],
            d["presentes"], d["faltas"], d["licencias"],
            f"{h_tot} hs {m_tot} min", str(hs_dec).replace('.', ',')
        ])

    output.seek(0)
    filename = f"Liquidacion_Muni_{desde}_al_{hasta}.csv"
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={filename}"})


# =====================================================================
# PORTAL WEB MUNICIPAL (/web)
# =====================================================================
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
                    <p class="text-xs text-purple-200">Terminal de Autogestión por DNI y Panel Administrativo</p>
                </div>
            </div>
            <div class="flex space-x-2">
                <button onclick="cambiarPestana('kiosco')" id="btn-tab-kiosco" class="bg-white text-purple-900 px-4 py-2 rounded-lg font-bold shadow">
                    👤 Empleados (Solo DNI)
                </button>
                <button onclick="cambiarPestana('rrhh')" id="btn-tab-rrhh" class="bg-purple-700 hover:bg-purple-600 text-white px-4 py-2 rounded-lg font-semibold">
                    🔐 Iniciar Sesión (Admin / RRHH)
                </button>
            </div>
        </nav>

        <!-- VISTA 1: TERMINAL EMPLEADOS (SOLO DNI, SIN REPORTE GLOBAL NI PDF) -->
        <div id="vista-kiosco" class="max-w-lg mx-auto mt-10 bg-white p-8 rounded-2xl shadow-xl border border-slate-200 text-center">
            <h2 class="text-2xl font-extrabold text-slate-800">Terminal de Personal</h2>
            <p class="text-slate-500 text-sm mt-1 mb-6">Ingrese únicamente su DNI para registrar asistencia o ver sus horas trabajadas</p>

            <div class="bg-slate-900 text-emerald-400 font-mono text-5xl font-bold py-4 rounded-xl mb-6 shadow-inner" id="reloj-vivo">
                00:00:00
            </div>

            <input type="number" id="dni-kiosco" placeholder="Ingrese su DNI sin puntos" autofocus
                class="w-full text-center text-3xl font-bold p-4 border-2 border-slate-300 rounded-xl mb-6 focus:border-purple-600 focus:outline-none">

            <div class="grid grid-cols-2 gap-4 mb-3">
                <button onclick="registrarFichaje('entrada')" class="bg-emerald-600 hover:bg-emerald-700 active:scale-95 transition text-white font-bold py-4 rounded-xl text-lg shadow-lg">
                    🟢 MARCAR ENTRADA
                </button>
                <button onclick="registrarFichaje('salida')" class="bg-orange-500 hover:bg-orange-600 active:scale-95 transition text-white font-bold py-4 rounded-xl text-lg shadow-lg">
                    🟠 MARCAR SALIDA
                </button>
            </div>
            <button onclick="registrarFichaje('consulta')" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-3 rounded-xl text-md shadow">
                ⏱️ VER MIS HORAS TRABAJADAS HOY Y EN EL MES
            </button>

            <div id="alerta-kiosco" class="mt-6 p-5 rounded-xl hidden text-left"></div>
        </div>

        <!-- VISTA 2: PANEL EXCLUSIVO ADMIN / RRHH (REQUIERE LOGIN) -->
        <div id="vista-rrhh" class="max-w-5xl mx-auto mt-8 px-4 hidden pb-16">
            
            <!-- FORMULARIO DE LOGIN -->
            <div id="caja-login-rrhh" class="max-w-md mx-auto bg-white p-8 rounded-2xl shadow-xl border border-slate-200">
                <div class="text-center mb-6">
                    <div class="text-4xl mb-2">🔐</div>
                    <h2 class="text-2xl font-bold text-slate-800">Ingreso Admin / RRHH</h2>
                    <p class="text-slate-500 text-sm">Los reportes de hoy, descargas PDF/Excel y carga de DNI son exclusivos para personal autorizado.</p>
                </div>
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-bold text-slate-700 mb-1">Usuario</label>
                        <input type="text" id="user-rrhh" class="w-full p-3 border rounded-lg" placeholder="Usuario Admin o RRHH">
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-700 mb-1">Contraseña</label>
                        <input type="password" id="pass-rrhh" class="w-full p-3 border rounded-lg" placeholder="••••••••">
                    </div>
                    <div id="error-login" class="text-red-600 text-sm font-bold hidden"></div>
                    <button onclick="iniciarSesionRRHH()" class="w-full bg-purple-800 hover:bg-purple-900 text-white font-bold py-3.5 rounded-xl shadow">
                        INGRESAR AL SISTEMA
                    </button>
                </div>
            </div>

            <!-- PANEL DESBLOQUEADO (REPORTES POR SECTOR + GESTIÓN DE DNI EN BASE DE DATOS) -->
            <div id="panel-descargas-rrhh" class="space-y-8 hidden">
                
                <!-- CABECERA DEL PANEL -->
                <div class="bg-white p-6 rounded-2xl shadow border flex flex-wrap justify-between items-center gap-4">
                    <div>
                        <span id="badge-rol" class="bg-purple-100 text-purple-800 text-xs font-extrabold px-3 py-1 rounded-full uppercase">ADMIN / RRHH</span>
                        <h2 class="text-2xl font-bold text-slate-800 mt-1">Panel de Control, Reportes y Base de Datos de DNI</h2>
                    </div>
                    <button onclick="cerrarSesionRRHH()" class="bg-red-600 hover:bg-red-700 text-white font-bold px-5 py-2.5 rounded-xl text-sm shadow">
                        Cerrar Sesión
                    </button>
                </div>

                <!-- BLOQUE 1: REPORTES DE HOY, PDF Y EXCEL FILTRADOS POR SECTOR -->
                <div class="bg-white p-6 rounded-2xl shadow border">
                    <h3 class="text-xl font-bold text-slate-800 mb-4">📊 1. Reportes de Asistencia y Liquidación (Filtro por Sector)</h3>
                    
                    <div class="grid grid-cols-1 md:grid-cols-3 gap-3 mb-4">
                        <button onclick="seleccionarPeriodo('dia')" class="bg-blue-50 hover:bg-blue-100 border border-blue-300 text-blue-900 font-bold py-3 rounded-xl">
                            📅 Día de Hoy
                        </button>
                        <button onclick="seleccionarPeriodo('semana')" class="bg-purple-50 hover:bg-purple-100 border border-purple-300 text-purple-900 font-bold py-3 rounded-xl">
                            📆 Últimos 7 Días (Semanal)
                        </button>
                        <button onclick="seleccionarPeriodo('mes')" class="bg-emerald-50 hover:bg-emerald-100 border border-emerald-300 text-emerald-900 font-bold py-3 rounded-xl">
                            📊 Mes Actual (Mensual)
                        </button>
                    </div>

                    <div class="grid grid-cols-1 md:grid-cols-3 gap-4 bg-slate-50 p-4 rounded-xl border mb-5">
                        <div>
                            <label class="block text-xs font-bold text-slate-700 mb-1">SELECCIONAR SECTOR:</label>
                            <select id="filtro-sector" class="w-full p-2.5 border-2 border-purple-300 rounded-lg bg-white font-semibold">
                                <option value="">Todos los sectores</option>
                            </select>
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-700 mb-1">FECHA DESDE (O DÍA DEL REPORTE):</label>
                            <input type="date" id="fecha-desde" class="w-full p-2.5 border rounded-lg bg-white">
                        </div>
                        <div>
                            <label class="block text-xs font-bold text-slate-700 mb-1">FECHA HASTA:</label>
                            <input type="date" id="fecha-hasta" class="w-full p-2.5 border rounded-lg bg-white">
                        </div>
                    </div>

                    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
                        <button onclick="verTablaEnVivo()" class="bg-slate-800 hover:bg-slate-900 text-white font-bold py-3.5 rounded-xl shadow">
                            👁️ VER REPORTE EN PANTALLA
                        </button>
                        <button onclick="descargarPDFAdmin()" class="bg-rose-600 hover:bg-rose-700 text-white font-bold py-3.5 rounded-xl shadow">
                            📄 DESCARGAR REPORTE PDF
                        </button>
                        <button onclick="descargarPlanillaHacienda()" class="bg-emerald-600 hover:bg-emerald-700 text-white font-bold py-3.5 rounded-xl shadow">
                            📥 DESCARGAR EXCEL HACIENDA
                        </button>
                    </div>

                    <!-- TABLA DE REPORTE EN PANTALLA -->
                    <div id="contenedor-tabla-vivo" class="mt-6 hidden">
                        <div class="overflow-x-auto border rounded-xl">
                            <table class="w-full text-left border-collapse text-sm">
                                <thead class="bg-slate-200 text-slate-700">
                                    <tr>
                                        <th class="p-3">Legajo</th>
                                        <th class="p-3">DNI</th>
                                        <th class="p-3">Empleado</th>
                                        <th class="p-3">Sector</th>
                                        <th class="p-3">Entrada</th>
                                        <th class="p-3">Salida (6hs)</th>
                                        <th class="p-3">Salida Real</th>
                                        <th class="p-3">Horas Trabajadas</th>
                                    </tr>
                                </thead>
                                <tbody id="tbody-vivo" class="divide-y"></tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <!-- BLOQUE 2: BASE DE DATOS DE EMPLEADOS (AGREGAR Y EDITAR DNI) -->
                <div class="bg-white p-6 rounded-2xl shadow border">
                    <h3 class="text-xl font-bold text-slate-800 mb-2">🪪 2. Base de Datos de Personal (Agregar / Editar DNI)</h3>
                    <p class="text-slate-500 text-sm mb-4">Registre nuevos empleados con su DNI o actualice los datos de cualquier trabajador.</p>

                    <div class="grid grid-cols-1 md:grid-cols-5 gap-3 bg-purple-50 p-4 rounded-xl border border-purple-200 mb-6">
                        <input type="hidden" id="emp-id-edit">
                        <input type="text" id="emp-nombre" placeholder="Apellido y Nombre" class="p-2.5 border rounded-lg bg-white">
                        <input type="number" id="emp-dni" placeholder="Número de DNI" class="p-2.5 border rounded-lg bg-white">
                        <input type="text" id="emp-legajo" placeholder="N° Legajo" class="p-2.5 border rounded-lg bg-white">
                        <select id="emp-sector" class="p-2.5 border rounded-lg bg-white"></select>
                        <button onclick="guardarEmpleadoWeb()" id="btn-guardar-emp" class="bg-purple-800 hover:bg-purple-900 text-white font-bold py-2.5 px-4 rounded-lg shadow">
                            ➕ Guardar DNI
                        </button>
                    </div>

                    <div class="flex justify-between items-center mb-3">
                        <input type="text" id="buscar-emp-web" oninput="filtrarEmpleadosWeb()" placeholder="🔍 Buscar en la base de datos por DNI, Nombre o Legajo..." class="w-full md:w-1/2 p-2.5 border rounded-lg">
                    </div>

                    <div class="overflow-x-auto border rounded-xl max-h-96">
                        <table class="w-full text-left border-collapse text-sm">
                            <thead class="bg-slate-200 text-slate-700 sticky top-0">
                                <tr>
                                    <th class="p-3">DNI</th>
                                    <th class="p-3">Legajo</th>
                                    <th class="p-3">Nombre Completo</th>
                                    <th class="p-3">Sector</th>
                                    <th class="p-3 text-center">Acción</th>
                                </tr>
                            </thead>
                            <tbody id="tbody-empleados" class="divide-y"></tbody>
                        </table>
                    </div>
                </div>

            </div>
        </div>

        <script>
            let tokenAdmin = localStorage.getItem('tarja_rrhh_token') || null;
            let listaEmpleadosGlobal = [];

            setInterval(() => {
                document.getElementById('reloj-vivo').innerText = new Date().toLocaleTimeString('es-AR', {hour12: false});
            }, 1000);

            function cambiarPestana(tab) {
                const esKiosco = (tab === 'kiosco');
                document.getElementById('vista-kiosco').classList.toggle('hidden', !esKiosco);
                document.getElementById('vista-rrhh').classList.toggle('hidden', esKiosco);
                if (!esKiosco) verificarEstadoLogin();
            }

            async function registrarFichaje(tipo) {
                const input = document.getElementById('dni-kiosco');
                const dni = input.value.trim();
                const alerta = document.getElementById('alerta-kiosco');

                if (!dni) {
                    alerta.className = "mt-6 p-4 rounded-xl bg-amber-100 text-amber-900 border border-amber-300 block font-bold text-center";
                    alerta.innerText = "⚠️ Ingrese su número de DNI.";
                    return;
                }

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
                            <div class="text-xs uppercase text-emerald-700 font-bold mb-2">Sector: ${data.sector}</div>
                            <div class="font-bold text-lg">${data.mensaje}</div>
                            <div class="mt-1 text-sm bg-white/80 p-2.5 rounded border border-emerald-300 font-semibold">${data.submensaje}</div>
                        `;
                        if (tipo !== 'consulta') input.value = "";
                    } else {
                        alerta.className = "mt-6 p-5 rounded-xl bg-red-100 text-red-900 border-2 border-red-300 block font-bold";
                        alerta.innerText = "❌ " + (data.detail || "No se pudo procesar.");
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
                        if (data.rol !== 'admin' && data.rol !== 'rrhh') {
                            errBox.innerText = "Acceso denegado: Solo usuarios Admin o RRHH pueden ver reportes globales.";
                            errBox.classList.remove('hidden');
                            return;
                        }
                        tokenAdmin = data.access_token;
                        localStorage.setItem('tarja_rrhh_token', tokenAdmin);
                        document.getElementById('badge-rol').innerText = "ROL ACTIVO: " + data.rol.toUpperCase();
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
                if (tipo === 'semana') desde.setDate(hoy.getDate() - 7);
                if (tipo === 'mes') desde = new Date(hoy.getFullYear(), hoy.getMonth(), 1);

                document.getElementById('fecha-desde').value = desde.toISOString().split('T')[0];
                document.getElementById('fecha-hasta').value = hoy.toISOString().split('T')[0];
            }

            async function verTablaEnVivo() {
                const fecha = document.getElementById('fecha-desde').value || new Date().toISOString().split('T')[0];
                const secFiltro = document.getElementById('filtro-sector').value;
                let url = `/admin/asistencias/${fecha}`;
                if (secFiltro) url += `?sector_id=${secFiltro}`;

                const res = await fetch(url, { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
                if (res.ok) {
                    const datos = await res.json();
                    const tbody = document.getElementById('tbody-vivo');
                    tbody.innerHTML = datos.length === 0 ? '<tr><td colspan="8" class="p-4 text-center text-slate-500">Sin registros para ese sector/fecha.</td></tr>' : '';
                    datos.forEach(a => {
                        tbody.innerHTML += `
                            <tr class="hover:bg-slate-50">
                                <td class="p-3 font-mono">${a.legajo || '-'}</td>
                                <td class="p-3 font-mono">${a.dni || '-'}</td>
                                <td class="p-3 font-bold">${a.nombre}</td>
                                <td class="p-3">${a.sector}</td>
                                <td class="p-3 text-emerald-700 font-semibold">${a.hora_llegada || '-'}</td>
                                <td class="p-3 text-slate-600">${a.salida_estimada || '-'}</td>
                                <td class="p-3 text-orange-600 font-semibold">${a.hora_salida || (a.en_curso ? 'Trabajando...' : '-')}</td>
                                <td class="p-3 font-bold ${a.en_curso ? 'text-blue-700' : 'text-emerald-800'}">${a.estado !== 'Presente' ? a.estado : a.tiempo_trabajado}</td>
                            </tr>
                        `;
                    });
                    document.getElementById('contenedor-tabla-vivo').classList.remove('hidden');
                }
            }

            async function descargarPDFAdmin() {
                const fecha = document.getElementById('fecha-desde').value;
                const sec = document.getElementById('filtro-sector').value;
                let url = `/admin/reporte_pdf?fecha=${fecha}`;
                if (sec) url += `&sector_id=${sec}`;

                const res = await fetch(url, { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
                if (res.ok) {
                    const blob = await res.blob();
                    const link = document.createElement('a');
                    link.href = window.URL.createObjectURL(blob);
                    link.download = `Reporte_Tarja_${fecha}.pdf`;
                    link.click();
                } else {
                    const err = await res.json();
                    alert(err.detail || "No hay datos para generar el PDF en ese sector.");
                }
            }

            async function descargarPlanillaHacienda() {
                const d = document.getElementById('fecha-desde').value;
                const h = document.getElementById('fecha-hasta').value;
                const sec = document.getElementById('filtro-sector').value;
                let url = `/rrhh/exportar-liquidacion?desde=${d}&hasta=${h}`;
                if (sec) url += `&sector_id=${sec}`;

                const res = await fetch(url, { headers: {'Authorization': 'Bearer ' + tokenAdmin} });
                if (res.ok) {
                    const blob = await res.blob();
                    const link = document.createElement('a');
                    link.href = window.URL.createObjectURL(blob);
                    link.download = `Liquidacion_Muni_${d}_al_${h}.csv`;
                    link.click();
                } else {
                    alert("Error al descargar Excel.");
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
                    tbody.innerHTML += `
                        <tr class="hover:bg-slate-50">
                            <td class="p-3 font-mono font-bold text-purple-900">${emp.dni}</td>
                            <td class="p-3 font-mono">${emp.legajo}</td>
                            <td class="p-3 font-semibold">${emp.nombre_completo}</td>
                            <td class="p-3">${emp.sector_nombre}</td>
                            <td class="p-3 text-center">
                                <button onclick='cargarParaEditar(${JSON.stringify(emp)})' class="text-blue-600 hover:underline font-bold text-xs">✏️ Editar DNI/Datos</button>
                            </td>
                        </tr>
                    `;
                });
            }

            function filtrarEmpleadosWeb() {
                const q = document.getElementById('buscar-emp-web').value.toLowerCase();
                const filtrados = listaEmpleadosGlobal.filter(e => 
                    e.dni.toLowerCase().includes(q) || 
                    e.nombre_completo.toLowerCase().includes(q) || 
                    e.legajo.toLowerCase().includes(q)
                );
                renderizarEmpleadosWeb(filtrados);
            }

            function cargarParaEditar(emp) {
                document.getElementById('emp-id-edit').value = emp.id;
                document.getElementById('emp-nombre').value = emp.nombre_completo;
                document.getElementById('emp-dni').value = emp.dni;
                document.getElementById('emp-legajo').value = emp.legajo;
                document.getElementById('emp-sector').value = emp.sector_id;
                document.getElementById('btn-guardar-emp').innerText = "💾 Actualizar Empleado";
            }

            async function guardarEmpleadoWeb() {
                const idEdit = document.getElementById('emp-id-edit').value;
                const nombre = document.getElementById('emp-nombre').value.trim();
                const dni = document.getElementById('emp-dni').value.trim();
                const legajo = document.getElementById('emp-legajo').value.trim();
                const sector_id = parseInt(document.getElementById('emp-sector').value);

                if (!nombre || !dni || !legajo || !sector_id) {
                    alert("Complete Nombre, DNI, Legajo y Sector.");
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
                    body: JSON.stringify({nombre_completo: nombre, dni: dni, legajo: legajo, sector_id: sector_id})
                });
                const data = await res.json();

                if (res.ok) {
                    alert(data.mensaje);
                    document.getElementById('emp-id-edit').value = '';
                    document.getElementById('emp-nombre').value = '';
                    document.getElementById('emp-dni').value = '';
                    document.getElementById('emp-legajo').value = '';
                    document.getElementById('btn-guardar-emp').innerText = "➕ Guardar DNI";
                    cargarEmpleadosWeb();
                } else {
                    alert("Error: " + (data.detail || "No se pudo guardar"));
                }
            }
        </script>
    </body>
    </html>
    """