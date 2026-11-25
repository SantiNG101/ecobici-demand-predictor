output "url_interfaz" {
  value = "https://app.${local.dominio}"
}

output "url_api" {
  value = "https://api.${local.dominio}/docs"
}

output "ip" {
  value = aws_eip.publica.public_ip
}

output "ssh" {
  value = "ssh ubuntu@${aws_eip.publica.public_ip}"
}

output "ver_arranque" {
  description = "Sigue la instalación del primer arranque (tarda unos 5 minutos)."
  value       = "ssh ubuntu@${aws_eip.publica.public_ip} 'tail -f /var/log/cloud-init-output.log'"
}

output "actualizar_codigo" {
  description = "Trae la rama y reconstruye. Para datos nuevos: terraform apply y después actualizar_datos."
  value       = "ssh ubuntu@${aws_eip.publica.public_ip} 'cd ${local.dir_servidor} && git pull && ${local.compose} up -d --build'"
}

output "actualizar_datos" {
  value = "ssh ubuntu@${aws_eip.publica.public_ip} 'cd ${local.dir_servidor} && aws s3 cp --recursive --region ${var.region} s3://${aws_s3_bucket.datos.id}/processed/ data/processed/ && ${local.compose} restart api'"
}
