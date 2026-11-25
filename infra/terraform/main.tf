data "http" "mi_ip" {
  url = "https://checkip.amazonaws.com"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

locals {
  ssh_cidr     = var.ssh_cidr != "" ? var.ssh_cidr : "${chomp(data.http.mi_ip.response_body)}/32"
  dir_datos    = abspath("${path.root}/${var.dir_datos}")
  archivos     = ["dataset_estacion_hora.parquet", "estaciones.csv"]
  dominio      = var.dominio != "" ? var.dominio : "${replace(aws_eip.publica.public_ip, ".", "-")}.sslip.io"
  dir_servidor = "/opt/ecobici"
  compose      = "docker compose -f docker-compose.yml -f infra/docker-compose.prod.yml"
}

# --- Datos: el parquet no está en git, así que pasa por un bucket privado ---

resource "aws_s3_bucket" "datos" {
  bucket_prefix = "ecobici-datos-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "datos" {
  bucket                  = aws_s3_bucket.datos.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "datos" {
  for_each = toset(local.archivos)

  bucket = aws_s3_bucket.datos.id
  key    = "processed/${each.key}"
  source = "${local.dir_datos}/${each.key}"
  etag   = filemd5("${local.dir_datos}/${each.key}")
}

# --- Permiso de la instancia para leer el bucket ---

resource "aws_iam_role" "servidor" {
  name_prefix = "ecobici-servidor-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "leer_datos" {
  role = aws_iam_role.servidor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.datos.arn },
      { Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.datos.arn}/*" },
    ]
  })
}

resource "aws_iam_instance_profile" "servidor" {
  name_prefix = "ecobici-servidor-"
  role        = aws_iam_role.servidor.name
}

# --- Red: sólo HTTP/HTTPS públicos (Caddy) y SSH desde una IP ---

resource "aws_security_group" "servidor" {
  name_prefix = "ecobici-servidor-"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [local.ssh_cidr]
  }

  ingress {
    description = "HTTP (desafio de Lets Encrypt y redireccion a HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_key_pair" "servidor" {
  key_name_prefix = "ecobici-"
  public_key      = file(pathexpand(var.clave_ssh_publica))
}

# --- Servidor ---

# La IP se crea antes que la instancia para que el dominio entre en user_data sin ciclos.
resource "aws_eip" "publica" {
  domain = "vpc"
}

resource "aws_instance" "servidor" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.tipo_instancia
  key_name               = aws_key_pair.servidor.key_name
  vpc_security_group_ids = [aws_security_group.servidor.id]
  iam_instance_profile   = aws_iam_instance_profile.servidor.name

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
  }

  metadata_options {
    http_tokens = "required"
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    repositorio  = var.repositorio
    rama         = var.rama
    bucket       = aws_s3_bucket.datos.id
    region       = var.region
    dominio      = local.dominio
    dir_servidor = local.dir_servidor
    compose      = local.compose
  })

  tags = {
    Name = "ecobici-servidor"
  }

  lifecycle {
    ignore_changes = [ami, user_data]
  }

  depends_on = [aws_s3_object.datos, aws_iam_role_policy.leer_datos]
}

resource "aws_eip_association" "publica" {
  instance_id   = aws_instance.servidor.id
  allocation_id = aws_eip.publica.id
}
