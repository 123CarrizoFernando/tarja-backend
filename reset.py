from database import engine
import models

print("Borrando tablas viejas...")
models.Base.metadata.drop_all(bind=engine)

print("Creando tablas nuevas con la estructura correcta...")
models.Base.metadata.create_all(bind=engine)

print("¡Base de datos reiniciada con éxito! Ya puedes cerrar este archivo.")