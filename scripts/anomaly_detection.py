import requests
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import seaborn as sns
from prophet import Prophet
from datetime import datetime, timedelta
import yaml
import os
import logging
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import openai

with open("../config/anomaly_detection_config.yaml", "r") as yamlfile:
    cfg = yaml.safe_load(yamlfile)

PROMETHEUS_URL = cfg['PROMETHEUS_URL']
OPENAI_API_KEY = cfg['OPENAI_API_KEY']
GRAFANA_DASHBOARD_URL = cfg['GRAFANA_DASHBOARD_URL']
DAYS_TO_INSPECT = cfg.get('DAYS_TO_INSPECT', 7)
DEVIATION_THRESHOLD = cfg.get('DEVIATION_THRESHOLD', 0.2)
K8S_SPIKE_THRESHOLD = cfg.get('K8S_SPIKE_THRESHOLD', 0.5)
EXCESS_DEVIATION_THRESHOLD = cfg.get('EXCESS_DEVIATION_THRESHOLD', 0.1)
CSV_OUTPUT = cfg.get('CSV_OUTPUT', False)
IMG_OUTPUT = cfg.get('IMG_OUTPUT', False)
GPT_ON = cfg.get('GPT_ON', False)
DOCKER = cfg.get('DOCKER', False)
QUERIES = cfg['QUERIES']

current_date = datetime.now()
date_str = current_date.strftime('%Y-%m-%d')
base_directory_name = f"outputs/{date_str}"


def sanitize_filename(s):
    s = str(s)
    hash_suffix = hashlib.md5(s.encode('utf-8')).hexdigest()[:8]
    s = s.replace('/', '_').replace('\\', '_').replace(':', '_').replace('\n', '_').replace(' ', '_')
    return s[:50] + '_' + hash_suffix


def setup_directories(metric_name):
    csv_dir = f"{base_directory_name}/csv_outputs/{sanitize_filename(metric_name)}"
    img_dir = f"{base_directory_name}/img_outputs/{sanitize_filename(metric_name)}"
    os.makedirs(csv_dir, exist_ok=True)
    os.makedirs(img_dir, exist_ok=True)
    return csv_dir, img_dir


openai.api_key = OPENAI_API_KEY


def fetch_prometheus_metrics(query, days):
    end = datetime.now()
    start = end - timedelta(days=days)
    params = {
        'query': query,
        'start': start.timestamp(),
        'end': end.timestamp(),
        'step': '1h'
    }
    try:
        response = requests.get(f'{PROMETHEUS_URL}/api/v1/query_range', params=params)
        response.raise_for_status()
        results = response.json().get('data', {}).get('result', [])
        return results
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to fetch metrics due to: {e}")
        return []


def process_metrics(results):
    dataframes = []
    for result in results:
        try:
            df = pd.DataFrame(result['values'], columns=['ds', 'y'])
            df['ds'] = pd.to_datetime(df['ds'], unit='s')
            df['y'] = pd.to_numeric(df['y'], errors='coerce')
            if 'metric' in result:
                for key in ['partner', 'path', 'pod']:
                    df[key] = result['metric'].get(key, 'unknown')
            dataframes.append(df)
        except Exception as e:
            logging.error(f"Error processing metrics: {e}")
    return dataframes


def detect_anomalies_with_prophet(dfs, deviation_threshold, excess_deviation_threshold, is_k8s=False):
    anomalies_list = []
    for df in dfs:
        try:
            if df.empty or len(df['y'].dropna()) < 2:
                logging.info("Insufficient data to fit model.")
                continue
            cp_scale = 0.05 if not is_k8s else 0.15
            m = Prophet(changepoint_prior_scale=cp_scale, yearly_seasonality=False, weekly_seasonality=True, daily_seasonality=is_k8s)
            m.fit(df[['ds', 'y']])
            future = m.make_future_dataframe(periods=24, freq='h')
            forecast = m.predict(future)
            forecast['fact'] = df['y'].reset_index(drop=True)

            forecast = forecast.join(df[['partner', 'path']], how='left')
            grouped = forecast.groupby(['partner', 'path'])

            for name, group in grouped:
                group['lower_bound'] = group['yhat'] - (group['yhat'] * deviation_threshold)
                group['upper_bound'] = group['yhat'] + (group['yhat'] * deviation_threshold)
                group['excessive_deviation'] = group['fact'] > (group['upper_bound'] + (group['upper_bound'] * excess_deviation_threshold))
                group['anomaly'] = (group['fact'] < group['lower_bound']) | (group['fact'] > group['upper_bound']) | group['excessive_deviation']

                anomalies = group[group['anomaly']]
                if not anomalies.empty:
                    anomalies_list.append((anomalies, name))
        except Exception as e:
            logging.error(f"Error detecting anomalies: {e}")
    return anomalies_list


def visualize_trends(anomalies_list, img_directory_name):
    for anomaly_info in anomalies_list:
        try:
            if isinstance(anomaly_info, tuple) and isinstance(anomaly_info[0], pd.DataFrame):
                anomalies, group_keys = anomaly_info
                plt.figure(figsize=(10, 6))
                partner, path = group_keys
                sanitized_partner = sanitize_filename(partner)
                sanitized_path = sanitize_filename(path)
                title = f"Trend and Anomalies for {sanitized_partner} Path: {sanitized_path}"
                filename = f"{img_directory_name}/trend_{sanitized_partner}_{sanitized_path}.png"
                plt.title(title)

                plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
                plt.gca().xaxis.set_major_locator(mdates.DayLocator())

                sns.lineplot(x=anomalies['ds'], y=anomalies['yhat'], label='Trend', color='blue')
                plt.fill_between(anomalies['ds'], anomalies['lower_bound'], anomalies['upper_bound'], color='green', alpha=0.3, label='Expected Range')

                normal_points = anomalies[~anomalies['excessive_deviation']]
                excessive_points = anomalies[anomalies['excessive_deviation']]
                sns.scatterplot(x=normal_points['ds'], y=normal_points['fact'], color='grey', marker='o', s=50, label='Normal')
                sns.scatterplot(x=excessive_points['ds'], y=excessive_points['fact'], color='red', marker='X', s=100, label='Excessive')

                plt.xlabel('Date')
                plt.ylabel('Metric Value')
                plt.legend(title='Point Type')

                plt.grid(True)
                plt.xticks(rotation=45)
                plt.tight_layout()
                plt.savefig(filename)
                plt.close()
        except Exception as e:
            logging.error(f"Error visualizing trends: {e}")


def analyze_with_chatgpt(anomalies):
    analysis_responses = []
    for anomaly in anomalies:
        prompt = f"Analyze the following anomalies: {anomaly}"
        try:
            response = openai.ChatCompletion.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "system", "content": "Analyze anomalies."}, {"role": "user", "content": prompt}]
            )
            analysis_responses.append(response.choices[0].message['content'])
        except Exception as e:
            logging.error(f"Error in ChatGPT analysis: {e}")
    return analysis_responses


def main():
    logging.basicConfig(level=getattr(logging, cfg.get('LOG_LEVEL', 'INFO')))
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(fetch_prometheus_metrics, QUERIES[metric_name], DAYS_TO_INSPECT): metric_name for metric_name in QUERIES}
        for future in as_completed(futures):
            metric_name = futures[future]
            result_data = future.result()
            if result_data:
                processed_data = process_metrics(result_data)
                csv_dir, img_dir = setup_directories(metric_name)
                is_k8s = "Kubernetes" in metric_name
                anomalies_list = detect_anomalies_with_prophet(processed_data, DEVIATION_THRESHOLD, EXCESS_DEVIATION_THRESHOLD, is_k8s=is_k8s)
                if anomalies_list and GPT_ON:
                    chat_gpt_responses = analyze_with_chatgpt([anomaly[0] for anomaly in anomalies_list])
                if anomalies_list:
                    visualize_trends(anomalies_list, img_dir)
                    if CSV_OUTPUT:
                        for anomalies, _, _ in anomalies_list:
                            filename = f"{csv_dir}/anomalies_{sanitize_filename(metric_name)}.csv"
                            anomalies.to_csv(filename, index=False)
                else:
                    logging.info("No anomalies detected for %s", metric_name)


if __name__ == "__main__":
    main()