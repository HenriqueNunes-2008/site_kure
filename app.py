import os
import psycopg2
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_bcrypt import Bcrypt
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from werkzeug.utils import secure_filename
import cloudinary
import cloudinary.uploader

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('app.secret_key')
bcrypt = Bcrypt(app)

# Configuração Cloudinary
cloudinary.config(secure=True)

# Configuração de Upload
UPLOAD_FOLDER = 'static/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Configuração do Banco de Dados Neon
DATABASE_URL = os.getenv('DATABASE_URL')

def get_db_connection():
    """Estabelece a conexão com o PostgreSQL do Neon."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# --- ROTAS DE NAVEGAÇÃO ---

@app.route('/')
def index():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Busca apenas produtos marcados para aparecer na home
            cur.execute("SELECT * FROM produtos WHERE na_home = TRUE ORDER BY criado_em DESC")
            produtos_home = cur.fetchall()
                # Busca configurações de imagens institucionais
            cur.execute("SELECT chave, valor FROM configuracoes")
            config = {row['chave']: row['valor'] for row in cur.fetchall()}
        return render_template('index.html', produtos=produtos_home, config=config)
    finally:
        conn.close()

@app.route('/cadastro.html')
def cadastro_page():
    return render_template('cadastro.html')

@app.route('/produtos.html')
def produtos_page():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.*, 
                       (SELECT json_agg(pi.imagem_url) FROM produto_imagens pi WHERE pi.produto_id = p.id) as galeria
                FROM produtos p 
                ORDER BY p.criado_em DESC
            """)
            todos_produtos = cur.fetchall()
        return render_template('produtos.html', produtos=todos_produtos)
    finally:
        conn.close()

@app.route('/dashboard.html')
def dashboard_page():
    # Apenas Admin ou Comprador podem acessar o dashboard
    if 'user_id' not in session or session.get('status') not in ['admin', 'comprador']:
        return redirect(url_for('index'))
    
    status = session.get('status')
    produtos = []
    pedidos = []
    config = {}
    
    if status == 'admin':
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT p.*, 
                           (SELECT json_agg(pi.imagem_url) FROM produto_imagens pi WHERE pi.produto_id = p.id) as galeria
                    FROM produtos p 
                    ORDER BY p.criado_em DESC
                """)
                produtos = cur.fetchall()
                
                # Busca as fotos atuais do site
                cur.execute("SELECT chave, valor FROM configuracoes")
                config = {row['chave']: row['valor'] for row in cur.fetchall()}
        finally:
            conn.close()
    
    elif status == 'comprador':
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                # Busca pedidos reais cruzando com usuários e produtos
                cur.execute("""
                    SELECT p.id, u.nome as cliente_nome, u.telefone as cliente_telefone, p.status,
                           pr.nome as produto_nome, pr.preco as produto_preco, p.quantidade, p.criado_em
                    FROM pedidos p
                    JOIN usuarios u ON p.usuario_id = u.id
                    JOIN produtos pr ON p.produto_id = pr.id
                    ORDER BY p.criado_em DESC
                """)
                pedidos = cur.fetchall()
                pedidos = [p for p in pedidos if p['status'] == 'pendente'] # Filtra apenas pedidos pendentes
        finally:
            conn.close()
            
    return render_template('dashboard.html', status=status, produtos=produtos, config=config, pedidos=pedidos)

# --- ROTAS DE API (BACKEND) ---

@app.route('/register', methods=['POST'])
def register():
    data = request.json
    # Transforma a senha em Hash antes de salvar
    hashed_pw = bcrypt.generate_password_hash(data['senha']).decode('utf-8')
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usuarios (nome, cpf, email, telefone, senha) VALUES (%s, %s, %s, %s, %s)",
                (data['nome'], data['cpf'], data['email'], data['telefone'], hashed_pw)
            )
            conn.commit()
        return jsonify({"message": "Usuário cadastrado com sucesso!"}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"error": "Erro ao cadastrar: Talvez o CPF ou E-mail já existam."}), 400
    finally:
        conn.close()

@app.route('/login', methods=['POST'])
def login():
    data = request.json
    email = data.get('email')
    senha = data.get('senha')
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # 1. Busca o usuário pelo e-mail
            cur.execute("SELECT * FROM usuarios WHERE email = %s", (email,))
            user = cur.fetchone()
            
            if user and bcrypt.check_password_hash(user['senha'], senha):
                # 2. Verifica se o CPF está na tabela de administradores
                cur.execute("SELECT status FROM administradores WHERE cpf = %s", (user['cpf'],))
                admin_data = cur.fetchone()
                
                status = admin_data['status'] if admin_data else 'usuario'
                
                if status == 'negado':
                    return jsonify({"error": "Acesso Negado"}), 403
                
                # 3. Cria a sessão
                session['user_id'] = str(user['id'])
                session['nome'] = user['nome']
                session['status'] = status
                
                return jsonify({"status": status, "nome": user['nome']}), 200
            
            return jsonify({"error": "E-mail ou senha incorretos."}), 401
    finally:
        conn.close()

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/fazer_pedido', methods=['POST'])
def fazer_pedido():
    if 'user_id' not in session:
        return jsonify({"error": "Login necessário"}), 401
    
    data = request.json
    user_id = session.get('user_id')
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pedidos (usuario_id, produto_id, quantidade) VALUES (%s, %s, %s)",
                (user_id, data['produto_id'], data['quantidade'])
            )
            conn.commit()
        return jsonify({"message": "Pedido enviado com sucesso!"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/admin/produto', methods=['POST'])
def add_produto():
    if 'user_id' not in session or session.get('status') != 'admin':
        return jsonify({"error": "Não autorizado"}), 403

    nome = request.form.get('nome')
    descricao = request.form.get('descricao')
    preco = request.form.get('preco')
    estoque = request.form.get('estoque')
    na_home = request.form.get('na_home') == 'on'
    
    file = request.files.get('imagem')
    filename = None
    if file and allowed_file(file.filename):
        upload_result = cloudinary.uploader.upload(file, folder="produtos")
        imagem_url = upload_result['secure_url']
    else:
        imagem_url = "placeholder.jpg"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO produtos (nome, descricao, preco, estoque, imagem_url, na_home) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (nome, descricao, preco, estoque, imagem_url, na_home)
            )
            produto_id = cur.fetchone()['id']
            
            # Processa Galeria (Até 10 fotos)
            galeria_files = request.files.getlist('galeria')
            for f in galeria_files[:10]:
                if f and allowed_file(f.filename):
                    f_upload = cloudinary.uploader.upload(f, folder="galeria")
                    cur.execute("INSERT INTO produto_imagens (produto_id, imagem_url) VALUES (%s, %s)", 
                                (produto_id, f_upload['secure_url']))
            
            conn.commit()
        return redirect(url_for('dashboard_page'))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/admin/produto/update/<id>', methods=['POST'])
def update_produto(id):
    if 'user_id' not in session or session.get('status') != 'admin':
        return jsonify({"error": "Não autorizado"}), 403

    nome = request.form.get('nome')
    descricao = request.form.get('descricao')
    preco = request.form.get('preco')
    estoque = request.form.get('estoque')
    na_home = request.form.get('na_home') == 'on'
    
    file = request.files.get('imagem')
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Se houver novas fotos de galeria, removemos as antigas e adicionamos as novas
            galeria_files = request.files.getlist('galeria')
            if any(f.filename != '' for f in galeria_files):
                cur.execute("DELETE FROM produto_imagens WHERE produto_id = %s", (id,))
                for f in galeria_files[:10]:
                    if f and allowed_file(f.filename):
                        f_upload = cloudinary.uploader.upload(f, folder="galeria")
                        cur.execute("INSERT INTO produto_imagens (produto_id, imagem_url) VALUES (%s, %s)", 
                                    (id, f_upload['secure_url']))

            if file and allowed_file(file.filename):
                upload_result = cloudinary.uploader.upload(file, folder="produtos")
                imagem_url = upload_result['secure_url']
                cur.execute(
                    """UPDATE produtos 
                       SET nome=%s, descricao=%s, preco=%s, estoque=%s, imagem_url=%s, na_home=%s 
                       WHERE id=%s""",
                    (nome, descricao, preco, estoque, imagem_url, na_home, id)
                )
            else:
                cur.execute(
                    """UPDATE produtos 
                       SET nome=%s, descricao=%s, preco=%s, estoque=%s, na_home=%s 
                       WHERE id=%s""",
                    (nome, descricao, preco, estoque, na_home, id)
                )
            conn.commit()
        return redirect(url_for('dashboard_page'))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/admin/produto/delete/<id>', methods=['POST'])
def delete_produto(id):
    if 'user_id' not in session or session.get('status') != 'admin':
        return jsonify({"error": "Não autorizado"}), 403

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Remove o produto. Como as páginas consultam a tabela 'produtos', 
            # ele sumirá automaticamente da Home e da listagem geral.
            cur.execute("DELETE FROM produtos WHERE id = %s", (id,))
            conn.commit()
        return redirect(url_for('dashboard_page'))
    except Exception as e:
        return jsonify({"error": "Erro ao excluir produto. Verifique se existem pedidos vinculados a ele."}), 400
    finally:
        conn.close()

@app.route('/admin/pedido/encerrar/<id>', methods=['POST'])
def encerrar_pedido(id):
    if 'user_id' not in session or session.get('status') not in ['admin', 'comprador']:
        return jsonify({"error": "Não autorizado"}), 403

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE pedidos SET status = 'concluido' WHERE id = %s", (id,))
            conn.commit()
        return redirect(url_for('dashboard_page'))
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/admin/configuracoes', methods=['POST'])
def update_configs():
    if 'user_id' not in session or session.get('status') != 'admin':
        return jsonify({"error": "Não autorizado"}), 403
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Processa as três possíveis fotos institucionais
            for key in ['foto_hero', 'foto_missao', 'foto_acao_social']:
                file = request.files.get(key)
                if file and allowed_file(file.filename):
                    upload_result = cloudinary.uploader.upload(file, folder="institucional")
                    imagem_url = upload_result['secure_url']
                    cur.execute("UPDATE configuracoes SET valor = %s WHERE chave = %s", (imagem_url, key))
            conn.commit()
        return redirect(url_for('dashboard_page'))
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

if __name__ == '__main__':
    app.run(debug=True)