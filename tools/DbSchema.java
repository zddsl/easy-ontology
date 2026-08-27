import java.sql.*;

/**
 * 枚举数据库表结构元数据，输出一行 JSON。
 *
 * 用法：DbSchema <jdbc-url> <user> <pass> <driverClass> <catalog> <schema>
 * - catalog/schema: 空串表示不限制（MySQL catalog=库名，DM8 schema=用户名大写）
 * - 输出：{"ok":true,"tables":[...]},{"ok":false,"error":"..."}
 */
public class DbSchema {
    public static void main(String[] args) {
        if (args.length < 4) {
            System.out.println("{\"ok\":false,\"error\":\"参数不足：DbSchema <jdbc-url> <user> <pass> <driverClass> [catalog] [schema]\"}");
            System.exit(1);
        }

        String url = args[0];
        String user = args[1];
        String pass = args[2];
        String driver = args[3];
        String catalog = args.length > 4 ? args[4] : null;
        String schema = args.length > 5 ? args[5] : null;

        // 处理空字符串参数
        if (catalog != null && catalog.isEmpty()) catalog = null;
        if (schema != null && schema.isEmpty()) schema = null;

        Connection conn = null;
        try {
            Class.forName(driver);
            long start = System.currentTimeMillis();
            conn = DriverManager.getConnection(url, user, pass);

            DatabaseMetaData meta = conn.getMetaData();
            StringBuilder json = new StringBuilder();
            json.append("{\"ok\":true,\"tables\":[");

            // 获取表列表（排除视图和系统表）
            // 达梦数据库可能表很多，设置合理的超时和限制
            Statement stmt = conn.createStatement();
            stmt.setQueryTimeout(25);  // 25秒超时，留5秒给JSON生成

            ResultSet tables = meta.getTables(catalog, schema, "%", new String[]{"TABLE"});
            boolean firstTable = true;
            int tableCount = 0;

            while (tables.next() && tableCount < 5000) {  // 上限5000张表
                // 定期检查是否超时，但给足时间处理大数据库
                if (System.currentTimeMillis() - start > 180000) {  // 3分钟超时
                    tables.close();
                    json.append("],\"ms\":").append(System.currentTimeMillis() - start);
                    json.append(",\"warning\":\"仅读取了前").append(tableCount).append("张表（超时限制），数据库表数量过多\"}");
                    System.out.println(json.toString());
                    conn.close();
                    return;
                }
                if (firstTable) {
                    firstTable = false;
                } else {
                    json.append(",");
                }

                String tableName = tables.getString("TABLE_NAME");
                json.append("{").append("\"name\":\"").append(esc(tableName)).append("\",\"columns\":[");

                // 获取列信息
                ResultSet cols = meta.getColumns(catalog, schema, tableName, "%");
                boolean firstCol = true;
                while (cols.next()) {
                    if (firstCol) {
                        firstCol = false;
                    } else {
                        json.append(",");
                    }

                    String colName = cols.getString("COLUMN_NAME");
                    int dataType = cols.getInt("DATA_TYPE");
                    String typeName = cols.getString("TYPE_NAME");
                    int nullable = cols.getInt("NULLABLE");

                    json.append("{")
                        .append("\"name\":\"").append(esc(colName)).append("\",")
                        .append("\"jdbc_type\":").append(dataType).append(",")
                        .append("\"type\":\"").append(mapType(dataType)).append("\",")
                        .append("\"type_name\":\"").append(esc(typeName)).append("\",")
                        .append("\"nullable\":").append(nullable == DatabaseMetaData.columnNullable)
                        .append("}");
                }
                cols.close();
                json.append("]}");
                tableCount++;
            }
            tables.close();

            json.append("],\"ms\":").append(System.currentTimeMillis() - start).append("}");

            System.out.println(json.toString());

        } catch (Exception e) {
            // 错误信息转义：双引号、换行、反斜杠
            String err = e.getMessage().replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "");
            System.out.println("{\"ok\":false,\"error\":\"" + err + "\"}");
        } finally {
            if (conn != null) {
                try {
                    conn.close();
                } catch (SQLException e) {
                    // ignore
                }
            }
        }
    }

    /** JDBC 类型映射到逻辑类型 */
    private static String mapType(int jdbcType) {
        switch (jdbcType) {
            case Types.BIGINT:
            case Types.INTEGER:
            case Types.SMALLINT:
            case Types.TINYINT:
                return "integer";
            case Types.DECIMAL:
            case Types.NUMERIC:
            case Types.DOUBLE:
            case Types.FLOAT:
            case Types.REAL:
                return "decimal";
            case Types.BIT:
            case Types.BOOLEAN:
                return "boolean";
            case Types.DATE:
                return "date";
            case Types.TIMESTAMP:
            case Types.TIME:
                return "datetime";
            case Types.BLOB:
            case Types.BINARY:
            case Types.VARBINARY:
            case Types.LONGVARBINARY:
                return "binary";
            default:
                return "string";  // VARCHAR、CHAR、TEXT、CLOB 等
        }
    }

    /** JSON 字符串转义（最小集） */
    private static String esc(String s) {
        if (s == null) return "";
        return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n").replace("\r", "").replace("\t", "\\t");
    }
}