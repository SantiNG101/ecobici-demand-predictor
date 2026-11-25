variable "region" {
  description = "Región de AWS. sa-east-1 (São Paulo) tiene menos latencia desde Buenos Aires pero es más cara."
  type        = string
  default     = "us-east-1"
}

variable "tipo_instancia" {
  description = "Con 1 GB (t3.micro) la API y la interfaz se quedan sin memoria; t3.small tiene 2 GB."
  type        = string
  default     = "t3.small"
}

variable "repositorio" {
  description = "Repositorio público que clona el servidor."
  type        = string
  default     = "https://github.com/SantiNG101/ecobici-demand-predictor.git"
}

variable "rama" {
  description = "Rama o tag a desplegar. Tiene que incluir infra/ (estar pusheada)."
  type        = string
  default     = "main"
}

variable "dir_datos" {
  description = "Carpeta local con dataset_estacion_hora.parquet y estaciones.csv. Se sube a S3 y el servidor la baja."
  type        = string
  default     = "../../data/processed"
}

variable "clave_ssh_publica" {
  description = "Clave pública para entrar por SSH como ubuntu."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "ssh_cidr" {
  description = "Desde dónde se permite SSH. Vacío: sólo desde la IP pública de quien corre terraform."
  type        = string
  default     = ""
}

variable "dominio" {
  description = "Dominio propio (con registros A de app.<dominio> y api.<dominio> a la IP). Vacío: <ip>.sslip.io."
  type        = string
  default     = ""
}
