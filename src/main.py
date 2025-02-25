from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import JSONResponse
from typing import List, Dict, Any, Optional
import pandas as pd
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from helpers.clustering_helpers import find_best_dbscan_params, remap_labels
from sklearn import metrics
from sentence_transformers import SentenceTransformer
import io
import json
import os
from pydantic import BaseModel

# Initialize FastAPI app
app = FastAPI(title="Data Analysis API", 
              description="API for clustering and similarity search operations")

# Create necessary directories
os.makedirs("uploads", exist_ok=True)
os.makedirs("results", exist_ok=True)

# Initialize the sentence transformer model for embeddings
model = SentenceTransformer('all-MiniLM-L6-v2')

class ClusteringParams(BaseModel):
    eps_range: List[float] = [0.1, 0.5, 1.0]
    min_samples_range: List[int] = [5, 10, 15]
    label_column_index: Optional[int] = -1  # Index of the label column, if it exists
    max_grid_search_combinations: Optional[int] = 9  # Limit grid search combinations

class SimilarityParams(BaseModel):
    text_column: str  # Column containing text to embed
    query_text: str = None  # Optional text to compare against
    top_k: int = 5  # Number of most similar items to return

@app.get("/")
async def root():
    return {"message": "Welcome to the Data Analysis API. Use /clustering or /similarity endpoints."}

@app.post("/clustering")
async def perform_clustering(
    file: UploadFile = File(...),
    params: ClusteringParams = Form(None)
):
    params = ClusteringParams.model_validate_json(params) if params else ClusteringParams()

    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="Only CSV files are supported")
    
    try:
        # Read the CSV file
        contents = await file.read()
        df = pd.read_csv(io.StringIO(contents.decode('utf-8')))
        
        # Define parameters
        if params is None:
            params = ClusteringParams()

        # Extract column names
        columns = df.columns.tolist()

        # Separate label column if specified
        labels_true = None
        if params.label_column_index is not None:
            if params.label_column_index >= len(columns):
                raise HTTPException(status_code=400, detail=f"Invalid label column index. Must be smaller than {len(columns)-1}")
            
            label_column = columns[params.label_column_index]
            labels_true = df[label_column].copy()
            df = df.drop(columns=[label_column])
            columns.remove(label_column)
        
        numeric_cols = []
        categorical_cols = []
        for column in columns:
            try:
                # Try to convert the column to numeric
                pd.to_numeric(df[column])
                numeric_cols.append(column)
            except ValueError:
                categorical_cols.append(column)
        
        # Create preprocessing pipeline
        preprocessor = ColumnTransformer(
            transformers=[
                ('num', SimpleImputer(strategy='mean'), numeric_cols),
                ('cat', OneHotEncoder(handle_unknown='ignore', sparse_output=False), categorical_cols)
            ],
            remainder='drop'
        )
        
        # Apply preprocessing
        features_preprocessed = preprocessor.fit_transform(df)

        scaler = StandardScaler()
        features_scaled = scaler.fit_transform(features_preprocessed)

        best_params, grid_search_results, best_labels = find_best_dbscan_params(
            features=features_scaled,
            eps_range=params.eps_range,
            min_samples_range=params.min_samples_range
        )
        all_cluster_ids = np.unique(best_labels)
        
        # Add cluster labels to the dataframe
        result_df = df.copy()
        result_df['cluster'] = best_labels

        additional_metrics = {"silhouette_coefficient": grid_search_results['combo_result']['metrics']['silhouette_score']}

        best_labels_remapped = remap_labels(labels_true, best_labels)
        if labels_true is not None:
            try:
                # Calculate the requested metrics
                additional_metrics = {
                    "homogeneity": float(metrics.homogeneity_score(labels_true, best_labels_remapped)),
                    "completeness": float(metrics.completeness_score(labels_true, best_labels_remapped)),
                    "v_measure": float(metrics.v_measure_score(labels_true, best_labels_remapped)),
                    "adjusted_rand_index": float(metrics.adjusted_rand_score(labels_true, best_labels_remapped)),
                    "adjusted_mutual_information": float(metrics.adjusted_mutual_info_score(labels_true, best_labels_remapped)),
                }
            except Exception as e:
                additional_metrics = {"error": str(e)}
        
        # Return results
        response = {
            "message": "Enhanced clustering completed successfully",
            "preprocessing": {
                "rows_before_processing": len(df),
                "numeric_columns": numeric_cols,
                "categorical_columns": categorical_cols,
                "label_column": columns[params.label_column_index] if params.label_column_index is not None else None
            },
            "grid_search": {
                "parameter_combinations_tested": len(grid_search_results),
                "best_parameters": best_params,
                "all_results": grid_search_results
            },
            "clustering_results": {
                "number_of_clusters": len(all_cluster_ids[all_cluster_ids != -1]),
                "noise_points": int(np.sum(best_labels == -1)),
                "noise_percentage": float(np.sum(best_labels == -1) / len(result_df) * 100),
                "additional_metrics": additional_metrics
            }
        }
        
        return response
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during clustering: {str(e)}")

@app.post("/similarity")
async def perform_similarity_search(
    file: UploadFile = File(...),
    params: SimilarityParams = None
):
    """
    Endpoint for text similarity search using vector embeddings.
    
    Upload a CSV file with a text column.
    Specify text_column, query_text (optional), and top_k parameters.
    """
    if not file.filename.endswith('.csv'):
        raise HTTPException(status_code=400, detail="Only CSV files are supported")
    
    if params is None:
        raise HTTPException(status_code=400, detail="Parameters required: at least text_column must be specified")
    
    try:
        # Read the CSV file
        contents = await file.read()
        df = pd.read_csv(io.StringIO(contents.decode('utf-8')))
        
        # Verify the text column exists
        if params.text_column not in df.columns:
            raise HTTPException(status_code=400, detail=f"Text column '{params.text_column}' not found in dataset")
            
        # Remove rows with missing text
        df = df.dropna(subset=[params.text_column])
        
        if df.empty:
            raise HTTPException(status_code=400, detail="No valid text data found in the specified column")
        
        # Create embeddings for the text column
        texts = df[params.text_column].tolist()
        embeddings = model.encode(texts)
        
        # If query text is provided, find similar items
        if params.query_text:
            # Encode the query
            query_embedding = model.encode([params.query_text])[0]
            
            # Calculate similarity scores
            similarities = metrics.pairwise.cosine_similarity([query_embedding], embeddings)[0]
            
            # Get top k similar items
            top_indices = similarities.argsort()[-params.top_k:][::-1]
            
            # Create result with similarities
            similar_items = []
            for idx in top_indices:
                similar_items.append({
                    "index": int(idx),
                    "text": texts[idx],
                    "similarity_score": float(similarities[idx]),
                    "original_row": json.loads(df.iloc[idx].to_json())
                })
                
            return {
                "message": "Similarity search completed successfully",
                "query": params.query_text,
                "similar_items": similar_items
            }
        
        # Otherwise just return info about the embedding process
        else:
            return {
                "message": "Vector embeddings created successfully",
                "total_records": len(df),
                "embedding_dimensions": embeddings.shape[1],
                "text_column": params.text_column,
                "note": "Submit a query_text parameter to perform similarity search"
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during similarity search: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)