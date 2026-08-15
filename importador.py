import csv
import os
from sqlalchemy.orm import Session
from passlib.context import CryptContext

# Importamos tu configuración actual
from database import SessionLocal, engine
import models

# Configuramos el encriptador de contraseñas (igual que en tu main.py)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_password_hash(password):
    return pwd_context.hash(password)

def importar_datos():
    db = SessionLocal()
    
    # 1. Definimos los 5 sectores y sus encargados
    # Podés cambiar los nombres de los sectores y usuarios acá
    configuracion_sectores = [
        {"sector": "Obras", "usuario": "juan.obras"},
        {"sector": "Pintura", "usuario": "maria.pintura"},
        {"sector": "Electricidad", "usuario": "pedro.electricidad"},
        {"sector": "Plomeria", "usuario": "luis.plomeria"},
        {"sector": "Logistica", "usuario": "ana.logistica"}
    ]

    print("--- INICIANDO IMPORTACIÓN MASIVA ---")

    # 2. Crear Sectores y Encargados
    for config in configuracion_sectores:
        nombre_sector = config["sector"]
        nombre_usuario = config["usuario"]

        # Buscamos si el sector ya existe, si no, lo creamos
        sector_db = db.query(models.Sector).filter(models.Sector.nombre == nombre_sector).first()
        if not sector_db:
            sector_db = models.Sector(nombre=nombre_sector)
            db.add(sector_db)
            db.commit()
            db.refresh(sector_db)
            print(f"✅ Sector creado: {nombre_sector}")

        # Buscamos si el encargado ya existe, si no, lo creamos
        encargado_db = db.query(models.Encargado).filter(models.Encargado.usuario == nombre_usuario).first()
        if not encargado_db:
            nuevo_encargado = models.Encargado(
                usuario=nombre_usuario,
                password_hash=get_password_hash("123456"), # Contraseña por defecto para todos
                sector_id=sector_db.id
            )
            db.add(nuevo_encargado)
            db.commit()
            print(f"👤 Encargado creado: {nombre_usuario} (Clave: 123456)")

    print("\n--- PROCESANDO EMPLEADOS ---")

    # 3. Leer el archivo CSV de empleados
    if not os.path.exists('empleados.csv'):
        print("❌ ERROR: No se encontró el archivo 'empleados.csv' en la carpeta.")
        return

    empleados_agregados = 0
    with open('empleados.csv', mode='r', encoding='utf-8') as archivo_csv:
        lector = csv.DictReader(archivo_csv)
        
        for fila in lector:
            dni = fila['dni'].strip()
            nombre = fila['nombre_completo'].strip()
            legajo = fila['legajo'].strip()
            nombre_sector_csv = fila['sector'].strip()

            # Evitar duplicados
            existe = db.query(models.Empleado).filter(models.Empleado.dni == dni).first()
            if existe:
                continue

            # Buscar el ID del sector que le pusiste en el Excel
            sector_vinculado = db.query(models.Sector).filter(models.Sector.nombre == nombre_sector_csv).first()
            
            if sector_vinculado:
                nuevo_empleado = models.Empleado(
                    dni=dni,
                    nombre_completo=nombre,
                    legajo=legajo,
                    sector_id=sector_vinculado.id
                )
                db.add(nuevo_empleado)
                empleados_agregados += 1
            else:
                print(f"⚠️ Atención: El sector '{nombre_sector_csv}' del empleado {nombre} no existe en la base de datos.")

    db.commit()
    print(f"\n🚀 ¡Éxito! Se importaron {empleados_agregados} empleados a la base de datos de Neon.")
    db.close()

if __name__ == "__main__":
    importar_datos()