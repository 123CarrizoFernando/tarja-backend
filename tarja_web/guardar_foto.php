<?php
header("Access-Control-Allow-Origin: *");
header("Access-Control-Allow-Methods: POST, OPTIONS");
header("Access-Control-Allow-Headers: Content-Type");
header("Content-Type: application/json; charset=UTF-8");

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(200);
    exit;
}

$input = json_decode(file_get_contents("php://input"), true);

if (!isset($input['foto_base64']) || !isset($input['dni']) || !isset($input['tipo'])) {
    http_response_code(400);
    echo json_encode(["error" => "Faltan datos requeridos"]);
    exit;
}

$dni = preg_replace('/[^0-9]/', '', $input['dni']);
$tipo = preg_replace('/[^a-z]/', '', strtolower($input['tipo']));
$foto_b64 = $input['foto_base64'];

// Extraer contenido binario del Base64
if (strpos($foto_b64, ',') !== false) {
    $partes = explode(',', $foto_b64);
    $foto_b64 = end($partes);
}
$binario = base64_decode($foto_b64);

if ($binario === false) {
    http_response_code(400);
    echo json_encode(["error" => "Imagen inválida"]);
    exit;
}

date_default_timezone_set('America/Argentina/Salta');
$fecha_hoy = date('Y-m-d');
$hora_str = date('His');

$directorio_base = __DIR__ . '/fotos_auditoria';
$directorio_dia = $directorio_base . '/' . $fecha_hoy;

if (!file_exists($directorio_dia)) {
    mkdir($directorio_dia, 0755, true);
}

// Limpieza automática de carpetas con más de 45 días de antigüedad
if (is_dir($directorio_base)) {
    $carpetas = scandir($directorio_base);
    $limite_tiempo = strtotime('-45 days');
    foreach ($carpetas as $carpeta) {
        if ($carpeta !== '.' && $carpeta !== '..' && is_dir($directorio_base . '/' . $carpeta)) {
            $fecha_carpeta = strtotime($carpeta);
            if ($fecha_carpeta && $fecha_carpeta < $limite_tiempo) {
                array_map('unlink', glob("$directorio_base/$carpeta/*.*"));
                @rmdir("$directorio_base/$carpeta");
            }
        }
    }
}

$nombre_archivo = "{$dni}_{$tipo}_{$hora_str}.jpg";
$ruta_completa = $directorio_dia . '/' . $nombre_archivo;

file_put_contents($ruta_completa, $binario);

// Armar la URL pública completa para guardar en la Base de Datos
$protocolo = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? "https://" : "http://";
$host = $_SERVER['HTTP_HOST'];
$ruta_web = rtrim(dirname($_SERVER['PHP_SELF']), '/\\');
$url_publica = "{$protocolo}{$host}{$ruta_web}/fotos_auditoria/{$fecha_hoy}/{$nombre_archivo}";

echo json_encode([
    "ok" => true,
    "url_foto" => $url_publica
]);
?>